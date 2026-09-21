import streamlit as st
import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re
import json
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# --- Scopes ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly"
]

def get_google_services():
    """Authenticate and return Google API service objects."""
    creds_dict = json.loads(st.secrets["gcp_service_account"])
    creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    
    gc = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    docs_service = build('docs', 'v1', credentials=creds)
    
    return gc, drive_service, docs_service

def extract_folder_id(url):
    """Extract Drive Folder ID from a standard URL."""
    match = re.search(r'folders/([a-zA-Z0-9_-]+)', url)
    return match.group(1) if match else None

def get_or_create_folder(drive_service, parent_id, folder_name):
    """Find a folder by name within a parent, or create it if missing."""
    query = f"'{parent_id}' in parents and name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    results = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
    
    if results:
        return results[0]['id']
    else:
        metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        new_folder = drive_service.files().create(body=metadata, fields='id').execute()
        return new_folder.get('id')

def count_words(docs_service, document_id):
    """Calculate word count for a Google Doc."""
    try:
        doc = docs_service.documents().get(documentId=document_id).execute()
        text = "".join([
            p_element.get('textRun').get('content') 
            for element in doc.get('body').get('content') if 'paragraph' in element
            for p_element in element.get('paragraph').get('elements') if 'textRun' in p_element
        ])
        return len(text.split())
    except Exception:
        return "N/A"

def send_notification_email(table_data):
    """Dispatch structured HTML email to the team."""
    sender = st.secrets["sender_email"]
    password = st.secrets["app_password"]
    receiver = "team_email@example.com"  # Update with your actual team/secretary email

    msg = MIMEMultipart("alternative")
    msg['Subject'] = "تحديث: مقالات جديدة جاهزة للترجمة"
    msg['From'] = sender
    msg['To'] = receiver

    html = """
    <html dir="rtl">
      <body style="font-family: Arial, sans-serif;">
        <h3 style="color: #2E86C1;">قائمة المقالات الجديدة:</h3>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%; text-align: center;">
          <tr style="background-color: #f2f2f2;">
            <th>عنوان المقال</th>
            <th>عدد الكلمات</th>
            <th>رابط مجلد العمل</th>
          </tr>
    """
    for row in table_data[1:]: 
        html += f"""
          <tr>
            <td>{row[0]}</td>
            <td>{row[1]}</td>
            <td><a href="{row[2]}" style="color: #E74C3C; text-decoration: none;"><b>افتح المجلد</b></a></td>
          </tr>
        """
    html += """
        </table>
      </body>
    </html>
    """
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
    server.login(sender, password)
    server.send_message(msg)
    server.quit()

def process_articles(coordinator_folder_id, selected_month, selected_year):
    """Main pipeline execution."""
    gc, drive_service, docs_service = get_google_services()
    root_folder_id = st.secrets["ROOT_TRANSLATION_FOLDER_ID"]
    sheet_id = st.secrets["SHEET_ID"]
    
    # 1. Fetch files from coordinator
    results = drive_service.files().list(
        q=f"'{coordinator_folder_id}' in parents and trashed = false", 
        fields="files(id, name)"
    ).execute()
    source_files = results.get('files', [])
    
    if not source_files:
        return None, "لا توجد ملفات في مجلد المنسق."

    # 2. Build Year and Month Hierarchy
    year_folder_name = f"{selected_year} Edition"
    month_name = datetime.date(selected_year, selected_month, 1).strftime('%B')
    month_folder_name = f"{selected_month:02d} - {month_name} {selected_year}"
    
    year_folder_id = get_or_create_folder(drive_service, root_folder_id, year_folder_name)
    month_folder_id = get_or_create_folder(drive_service, year_folder_id, month_folder_name)

    # 3. Setup Sheet Data
    sheet = gc.open_by_key(sheet_id).worksheet("Translation_Tracker")
    existing_data = sheet.get_all_values()
    table_data = existing_data if existing_data else [["عنوان المقال", "عدد الكلمات", "رابط مجلد العمل"]]
    
    processed_any = False

    # 4. Process each file
    for file in source_files:
        file_name = file['name'].replace('.docx', '')
        
        # Check if article folder already exists to prevent duplicates
        query = f"'{month_folder_id}' in parents and name = '{file_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        existing_article = drive_service.files().list(q=query, fields="files(id)").execute().get('files', [])
        
        if not existing_article:
            processed_any = True
            
            # Create Article Master Folder
            article_folder_id = get_or_create_folder(drive_service, month_folder_id, file_name)
            
            # Create the 4 Stage Subfolders
            stage0_id = get_or_create_folder(drive_service, article_folder_id, "00- المادة الأصلية (السكرتير)")
            get_or_create_folder(drive_service, article_folder_id, "01- ترجمة المقالات (المترجمون)")
            get_or_create_folder(drive_service, article_folder_id, "02- تدقيق الترجمة (مدققو الترجمة)")
            get_or_create_folder(drive_service, article_folder_id, "03- تسجيل المقالات (المسجلون)")
            
            # Copy file to Stage 00
            copied_file = {'parents': [stage0_id]}
            copy_result = drive_service.files().copy(
                fileId=file['id'], 
                body=copied_file,
                fields="id"
            ).execute()
            
            folder_link = f"https://drive.google.com/drive/folders/{article_folder_id}"
            word_count = count_words(docs_service, copy_result['id'])
            table_data.append([file_name, word_count, folder_link])

    if processed_any:
        sheet.clear()
        sheet.update(range_name='A1', values=table_data)
        try:
            send_notification_email(table_data)
        except Exception as e:
            return None, f"تمت المعالجة لكن فشل إرسال الإيميل: {e}"
        return table_data, "تم سحب المقالات، بناء المجلدات، وتحديث الشيت بنجاح."
    else:
        return None, "جميع المقالات موجودة مسبقاً، لم يتم إضافة جديد."

# --- Streamlit UI ---
st.title("بوابة استلام مقالات الترجمة")

coordinator_url = st.text_input("رابط مجلد المنسق (Google Drive):")

col1, col2 = st.columns(2)
with col1:
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    selected_month_name = st.selectbox("شهر الإصدار:", months, index=datetime.datetime.now().month - 1)
    selected_month_num = months.index(selected_month_name) + 1
with col2:
    current_year = datetime.datetime.now().year
    selected_year = st.selectbox("سنة الإصدار:", range(current_year - 1, current_year + 5), index=1)

if st.button("سحب المقالات وبدء العمل"):
    if not coordinator_url:
        st.warning("يرجى إدخال رابط المنسق أولاً.")
    else:
        source_folder_id = extract_folder_id(coordinator_url)
        if not source_folder_id:
            st.error("الرابط غير صحيح. تأكد من أنه رابط مجلد جوجل درايف.")
        else:
            with st.spinner("جاري معالجة الملفات وبناء هيكل المجلدات..."):
                try:
                    data, msg = process_articles(source_folder_id, selected_month_num, selected_year)
                    if data:
                        st.success(msg)
                        st.balloons()
                    else:
                        st.info(msg)
                except Exception as e:
                    st.error(f"حدث خطأ أثناء التشغيل: {e}")
