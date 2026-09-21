import streamlit as st
import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re
import json
import gspread
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# --- الصلاحيات المطلوبة ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly"
]

def get_google_services():
    """تهيئة الاتصال بخدمات جوجل باستخدام مفتاح OAuth الشخصي"""
    creds_dict = json.loads(st.secrets["gcp_oauth_token"])
    creds = Credentials.from_authorized_user_info(creds_dict, SCOPES)
    
    gc = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    docs_service = build('docs', 'v1', credentials=creds)
    
    return gc, drive_service, docs_service

def extract_folder_id(url):
    """استخراج المعرف (ID) من رابط جوجل درايف"""
    match = re.search(r'folders/([a-zA-Z0-9_-]+)', url)
    return match.group(1) if match else None

def clean_title(raw_title):
    """تنظيف العنوان من الفوضى والمسافات الزائدة واستخراج النصوص بدقة"""
    title = raw_title.replace('.docx', '').replace('.doc', '')
    title = re.sub(r'\s+', ' ', title).strip()
    return title

def get_or_create_folder(drive_service, parent_id, folder_name):
    """البحث الذكي عن مجلد وإنشاؤه فقط إن لم يكن موجوداً لتجنب التكرار والفوضى"""
    cleaned_name = clean_title(folder_name)
    safe_folder_name = cleaned_name.replace("'", "\\'")
    
    query = f"'{parent_id}' in parents and name = '{safe_folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
    
    if results:
        return results[0]['id']
    else:
        metadata = {
            'name': cleaned_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        new_folder = drive_service.files().create(body=metadata, fields='id').execute()
        return new_folder.get('id')

def count_words(docs_service, document_id):
    """حساب عدد الكلمات بدقة من مستند جوجل متجاوزاً أي تنسيقات معقدة"""
    try:
        doc = docs_service.documents().get(documentId=document_id).execute()
        text = "".join([
            p_element.get('textRun').get('content') 
            for element in doc.get('body').get('content') if 'paragraph' in element
            for p_element in element.get('paragraph').get('elements') if 'textRun' in p_element
        ])
        words = re.findall(r'\b\w+\b', text)
        return len(words)
    except Exception:
        return "N/A"

def send_notification_email(table_data):
    """إرسال إيميل التنبيه لعدة مستخدمين دفعة واحدة"""
    sender = st.secrets["sender_email"]
    password = st.secrets["app_password"]
    
    # قائمة المستقبلين مفصولة بفاصلة
    recipients_list = [
        "arabicessaytranslation@gmail.com", 
        "ameermam.sa@gmail.com", 
        "mohammedd9644@gmail.com", 
        "keepcomingback.29@gmail.com", 
        "ahmad2075533@gmail.com"
    ]

    msg = MIMEMultipart("alternative")
    msg['Subject'] = "تحديث: مقالات جديدة جاهزة للعمل المشترك"
    msg['From'] = sender
    msg['To'] = ", ".join(recipients_list)

    html = """
    <html dir="rtl">
      <body style="font-family: Arial, sans-serif;">
        <h3 style="color: #2E86C1;">قائمة المقالات الجديدة:</h3>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%; text-align: center;">
          <tr style="background-color: #f2f2f2;">
            <th>عنوان المقال</th>
            <th>عدد الكلمات</th>
            <th>رابط مجلد المقال</th>
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
    server.sendmail(sender, recipients_list, msg.as_string())
    server.quit()

def process_articles(coordinator_folder_id, selected_month, selected_year):
    """محرك العمل الذكي (بدون تكرار، مجلد واحد لكل مقال، ونسخة مشتركة)"""
    gc, drive_service, docs_service = get_google_services()
    root_folder_id = st.secrets["ROOT_TRANSLATION_FOLDER_ID"]
    sheet_id = st.secrets["SHEET_ID"]
    
    # 1. جلب الملفات من مجلد المنسق
    results = drive_service.files().list(
        q=f"'{coordinator_folder_id}' in parents and trashed = false", 
        fields="files(id, name)"
    ).execute()
    source_files = results.get('files', [])
    
    if not source_files:
        return None, "لا توجد ملفات في مجلد المنسق."

    # 2. بناء مجلد السنة والشهر
    year_folder_name = f"{selected_year} Edition"
    month_name = datetime.date(selected_year, selected_month, 1).strftime('%B')
    month_folder_name = f"{selected_month:02d} - {month_name} {selected_year}"
    
    year_folder_id = get_or_create_folder(drive_service, root_folder_id, year_folder_name)
    month_folder_id = get_or_create_folder(drive_service, year_folder_id, month_folder_name)

    # 3. إعداد ملف الإكسل
    sheet = gc.open_by_key(sheet_id).worksheet("Translation_Tracker")
    existing_data = sheet.get_all_values()
    table_data = existing_data if existing_data else [["عنوان المقال", "عدد الكلمات", "رابط مجلد العمل"]]
    
    processed_any = False

    # 4. معالجة كل ملف بتنظيف أسمائه والتحقق من عدم تكراره
    for file in source_files:
        clean_name = clean_title(file['name'])
        
        # التحقق الذكي من عدم تكرار المجلد للمقال
        safe_file_name = clean_name.replace("'", "\\'")
        query_folder = f"'{month_folder_id}' in parents and name = '{safe_file_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        existing_folders = drive_service.files().list(q=query_folder, fields="files(id)").execute().get('files', [])
        
        if not existing_folders:
            processed_any = True
            
            # أ. إنشاء مجلد واحد نظيف للمقال
            article_folder_id = get_or_create_folder(drive_service, month_folder_id, clean_name)
            
            # ب. التأكد من نسخ المستند مرة واحدة فقط داخل المجلد المشترك
            query_file = f"'{article_folder_id}' in parents and trashed = false"
            existing_files = drive_service.files().list(q=query_file, fields="files(id)").execute().get('files', [])
            
            if not existing_files:
                copied_file = {'parents': [article_folder_id]}
                copy_result = drive_service.files().copy(
                    fileId=file['id'], 
                    body=copied_file,
                    fields="id"
                ).execute()
                doc_id = copy_result['id']
            else:
                doc_id = existing_files[0]['id']
            
            # ج. حساب الكلمات وتسجيل البيانات في الإكسل
            folder_link = f"https://drive.google.com/drive/folders/{article_folder_id}"
            word_count = count_words(docs_service, doc_id)
            table_data.append([clean_name, word_count, folder_link])

    if processed_any and len(table_data) > len(existing_data):
        sheet.clear()
        sheet.update(range_name='A1', values=table_data)
        try:
            send_notification_email(table_data)
        except Exception as e:
            return None, f"تمت المعالجة بنجاح، لكن فشل إرسال الإيميل بسبب: {e}"
        return table_data, "تم سحب المقالات، تنظيف العناوين، وتحديث الشيت وإرسال التنبيهات بنجاح."
    else:
        return None, "جميع المقالات موجودة مسبقاً، لم يتم إضافة أي جديد."

# ==========================================
# واجهة المستخدم (Streamlit Frontend)
# ==========================================
st.set_page_config(page_title="بوابة استلام المقالات الذكية", page_icon="📝")
st.title("بوابة استلام مقالات الترجمة (العمل التشاركي)")

coordinator_url = st.text_input("رابط مجلد المنسق (Google Drive):")

col1, col2 = st.columns(2)
with col1:
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    selected_month_name = st.selectbox("شهر الإصدار:", months, index=datetime.datetime.now().month - 1)
    selected_month_num = months.index(selected_month_name) + 1
with col2:
    current_year = datetime.datetime.now().year
    selected_year = st.selectbox("سنة الإصدار:", range(current_year - 1, current_year + 5), index=1)

if st.button("سحب المقالات الذكية"):
    if not coordinator_url:
        st.warning("يرجى إدخال رابط المنسق أولاً.")
    else:
        source_folder_id = extract_folder_id(coordinator_url)
        if not source_folder_id:
            st.error("الرابط غير صحيح. تأكد من أنه رابط مجلد جوجل درايف.")
        else:
            with st.spinner("جاري تنظيف الأسماء، فحص المجلدات، وحساب الكلمات بدقة..."):
                try:
                    data, msg = process_articles(source_folder_id, selected_month_num, selected_year)
                    if data:
                        st.success(msg)
                        st.balloons()
                    else:
                        st.info(msg)
                except Exception as e:
                    st.error(f"حدث خطأ أثناء التشغيل: {e}")
