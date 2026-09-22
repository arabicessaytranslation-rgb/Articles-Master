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

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly"
]

def get_google_services():
    """تهيئة الاتصال بخدمات جوجل عبر OAuth الشخصي"""
    creds_dict = json.loads(st.secrets["gcp_oauth_token"])
    creds = Credentials.from_authorized_user_info(creds_dict, SCOPES)
    
    gc = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    docs_service = build('docs', 'v1', credentials=creds)
    
    return gc, drive_service, docs_service

def extract_folder_id(url):
    """استخراج المعرف من رابط جوجل درايف"""
    match = re.search(r'folders/([a-zA-Z0-9_-]+)', url)
    return match.group(1) if match else None

def clean_title(raw_title):
    """تنظيف اسم الملف من الامتدادات والمسافات الزائدة"""
    title = re.sub(r'\.(docx|doc|gdoc)$', '', raw_title, flags=re.IGNORECASE)
    title = re.sub(r'\s+', ' ', title).strip()
    return title

def find_or_create_subfolder(drive_service, parent_id, target_name, match_patterns):
    """
    البحث بمرونة داخل المجلد الأب عن أي مجلد مطابق لأحد الأنماط لتجنب إنشاء مجلد مكرر.
    إذا لم يجد أي تطابق، يقوم بإنشاء مجلد جديد بالاسم target_name.
    """
    query = f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    response = drive_service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    
    existing_folders = response.get('files', [])
    
    # 1. فحص المجلدات الموجودة ومطابقتها بمرونة
    for folder in existing_folders:
        f_name_lower = folder['name'].lower().strip()
        for pattern in match_patterns:
            if pattern.lower() in f_name_lower:
                return folder['id']
                
    # 2. إذا لم يكن موجوداً، يتم إنشاؤه
    metadata = {
        'name': target_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    new_folder = drive_service.files().create(
        body=metadata,
        fields='id',
        supportsAllDrives=True
    ).execute()
    return new_folder.get('id')

def count_words(docs_service, document_id):
    """حساب عدد الكلمات من مستند جوجل"""
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
    """إرسال إيميل التنبيه بروابط مباشرة للمستندات"""
    sender = st.secrets["sender_email"]
    password = st.secrets["app_password"]
    
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
        <h3 style="color: #2E86C1;">قائمة المقالات الجديدة المضافة:</h3>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%; text-align: center;">
          <tr style="background-color: #f2f2f2;">
            <th>عنوان المقال</th>
            <th>عدد الكلمات</th>
            <th>رابط فتح المستند مباشرة</th>
          </tr>
    """
    for row in table_data[1:]: 
        html += f"""
          <tr>
            <td style="text-align: right; padding-right: 12px;">{row[0]}</td>
            <td>{row[1]}</td>
            <td><a href="{row[2]}" style="color: #2980B9; text-decoration: none; font-weight: bold;">افتح المستند</a></td>
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
    """معالجة المقالات ونسخها مباشرة لمجلد الشهر بدون مجلدات وسيطة زائدة"""
    gc, drive_service, docs_service = get_google_services()
    root_folder_id = st.secrets["ROOT_TRANSLATION_FOLDER_ID"]
    sheet_id = st.secrets["SHEET_ID"]
    
    # 1. جلب الملفات من مجلد المنسق
    query_source = f"'{coordinator_folder_id}' in parents and trashed = false and mimeType != 'application/vnd.google-apps.folder'"
    results = drive_service.files().list(
        q=query_source, 
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    source_files = results.get('files', [])
    
    if not source_files:
        return None, "لا توجد ملفات في مجلد المنسق."

    # 2. تحديد أو إنشاء مجلد السنة بمرونة (يبحث عن 2026 أو 2026 Edition)
    year_target = f"{selected_year} Edition"
    year_patterns = [str(selected_year), f"{selected_year} Edition"]
    year_folder_id = find_or_create_subfolder(drive_service, root_folder_id, year_target, year_patterns)

    # 3. تحديد أو إنشاء مجلد الشهر بمرونة (يبحث عن October أو 10 - October)
    month_name = datetime.date(selected_year, selected_month, 1).strftime('%B')
    month_target = f"{selected_month:02d} - {month_name} {selected_year}"
    month_patterns = [month_name, f"{selected_month:02d} - {month_name}", f"{selected_month:02d}"]
    month_folder_id = find_or_create_subfolder(drive_service, year_folder_id, month_target, month_patterns)

    # 4. جلب أسماء الملفات الموجودة حالياً داخل مجلد الشهر لمنع التكرار
    query_month_files = f"'{month_folder_id}' in parents and trashed = false"
    existing_month_files_res = drive_service.files().list(
        q=query_month_files,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    existing_file_names = {clean_title(f['name']).lower() for f in existing_month_files_res.get('files', [])}

    # 5. قراءة ملف الإكسل
    sheet = gc.open_by_key(sheet_id).worksheet("Translation_Tracker")
    existing_sheet_data = sheet.get_all_values()
    table_data = existing_sheet_data if existing_sheet_data else [["عنوان المقال", "عدد الكلمات", "رابط المستند"]]
    
    processed_count = 0

    # 6. نسخ الملفات مباشرة إلى مجلد الشهر
    for file in source_files:
        clean_name = clean_title(file['name'])
        
        # تخطي الملف إذا كان موجوداً مسبقاً بنفس الاسم
        if clean_name.lower() in existing_file_names:
            continue
            
        # نسخ الملف مباشرة داخل مجلد الشهر
        copied_file_metadata = {
            'name': clean_name,
            'parents': [month_folder_id]
        }
        copy_result = drive_service.files().copy(
            fileId=file['id'], 
            body=copied_file_metadata,
            fields="id",
            supportsAllDrives=True
        ).execute()
        
        doc_id = copy_result['id']
        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        word_count = count_words(docs_service, doc_id)
        
        table_data.append([clean_name, word_count, doc_url])
        existing_file_names.add(clean_name.lower())
        processed_count += 1

    # 7. تحديث الإكسل وإرسال الإيميل إن وجدت ملفات جديدة
    if processed_count > 0:
        sheet.clear()
        sheet.update(range_name='A1', values=table_data)
        try:
            send_notification_email(table_data)
        except Exception as e:
            return table_data, f"تم نسخ {processed_count} مقال وتحديث الإكسل، لكن تعذر إرسال الإيميل: {e}"
        return table_data, f"تم بنجاح نسخ {processed_count} مقال مباشرة داخل مجلد الشهر، وتحديث الإكسل، وإرسال التنبيهات."
    else:
        return None, "جميع المقالات موجودة بالفعل داخل مجلد هذا الشهر. لم تتم إضافة ملفات جديدة."

# ==========================================
# واجهة المستخدم (Streamlit Frontend)
# ==========================================
st.set_page_config(page_title="بوابة استلام المقالات", page_icon="📝")
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
            st.error("الرابط غير صحيح. يرجى التأكد من نسخ رابط مجلد Google Drive يحوي folders/ID.")
        else:
            with st.spinner("جاري فحص المجلد الرئيسي ومعالجة المقالات..."):
                try:
                    data, msg = process_articles(source_folder_id, selected_month_num, selected_year)
                    if data:
                        st.success(msg)
                        st.balloons()
                    else:
                        st.info(msg)
                except Exception as e:
                    st.error(f"حدث خطأ أثناء التشغيل: {e}")
