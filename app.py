import streamlit as st
import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re
import json
from difflib import SequenceMatcher
import gspread
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# --- Google API Scopes ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly"
]

def get_google_services():
    """Initializes Google API clients via stored OAuth token."""
    creds_dict = json.loads(st.secrets["gcp_oauth_token"])
    creds = Credentials.from_authorized_user_info(creds_dict, SCOPES)
    
    gc = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    docs_service = build('docs', 'v1', credentials=creds)
    
    return gc, drive_service, docs_service

def extract_folder_id(url):
    """Extracts Drive folder ID from full URL."""
    match = re.search(r'folders/([a-zA-Z0-9_-]+)', url)
    return match.group(1) if match else None

def normalize_string(val):
    """Cleans and standardizes text for fuzzy matching."""
    val = re.sub(r'[^a-zA-Z0-9\u0600-\u06FF\s]', ' ', val)
    return re.sub(r'\s+', ' ', val).strip().lower()

def list_subfolders(drive_service, parent_id):
    """Lists non-trashed subfolders inside a specific parent folder."""
    query = f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    response = drive_service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        pageSize=100
    ).execute()
    return response.get('files', [])

def detect_existing_folders(drive_service, root_folder_id, target_year, target_month_num):
    """
    Checks if Year and Month folders already exist (exact or similar).
    Returns inspection metadata without creating or modifying anything.
    """
    # 1. Inspect Year Folder
    year_folders = list_subfolders(drive_service, root_folder_id)
    year_str = str(target_year)
    matched_year = None

    for folder in year_folders:
        tokens = re.findall(r'\b\d{4}\b', folder['name'])
        if year_str in tokens or year_str in folder['name']:
            matched_year = folder
            break

    if not matched_year:
        for folder in year_folders:
            sim = SequenceMatcher(None, normalize_string(f"{target_year} Edition"), normalize_string(folder['name'])).ratio()
            if sim >= 0.70:
                matched_year = folder
                break

    # 2. Inspect Month Folder if Year folder exists
    matched_month = None
    if matched_year:
        month_folders = list_subfolders(drive_service, matched_year['id'])
        month_date = datetime.date(target_year, target_month_num, 1)
        full_name = month_date.strftime('%B')
        short_name = month_date.strftime('%b')
        target_num_str = str(target_month_num)
        target_num_padded = f"{target_month_num:02d}"

        highest_score = 0.0
        for folder in month_folders:
            name_clean = normalize_string(folder['name'])
            tokens = name_clean.split()
            
            has_name = (full_name.lower() in name_clean) or (short_name.lower() in tokens)
            nums = re.findall(r'\b\d{1,2}\b', folder['name'])
            has_num = (target_num_str in nums) or (target_num_padded in nums)

            if has_name or has_num:
                sim = SequenceMatcher(None, normalize_string(f"{target_num_padded} - {full_name} {target_year}"), name_clean).ratio()
                if sim > highest_score:
                    highest_score = sim
                    matched_month = folder

    return {
        "year_exists": matched_year is not None,
        "year_folder": matched_year,
        "month_exists": matched_month is not None,
        "month_folder": matched_month
    }

def get_or_create_folder(drive_service, parent_id, folder_name):
    """Creates a folder under parent if not already passed directly."""
    metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    created = drive_service.files().create(body=metadata, fields='id', supportsAllDrives=True).execute()
    return created.get('id')

def count_words(docs_service, document_id):
    """Counts words from a Google Doc body."""
    try:
        doc = docs_service.documents().get(documentId=document_id).execute()
        text = "".join([
            p_elem.get('textRun', {}).get('content', '')
            for elem in doc.get('body', {}).get('content', []) if 'paragraph' in elem
            for p_elem in elem.get('paragraph', {}).get('elements', []) if 'textRun' in p_elem
        ])
        return len(re.findall(r'\b\w+\b', text))
    except Exception:
        return "N/A"

def send_notification_email(table_data):
    """Sends notification email to specified team recipients."""
    sender = st.secrets["sender_email"]
    password = st.secrets["app_password"]
    
    recipients = [
        "arabicessaytranslation@gmail.com",
        "ameermam.sa@gmail.com",
        "mohammedd9644@gmail.com",
        "keepcomingback.29@gmail.com",
        "ahmad2075533@gmail.com"
    ]

    msg = MIMEMultipart("alternative")
    msg['Subject'] = "تحديث: مقالات جديدة جاهزة للعمل المشترك"
    msg['From'] = sender
    msg['To'] = ", ".join(recipients)

    html = """
    <html dir="rtl">
      <body style="font-family: Arial, sans-serif;">
        <h3 style="color: #2E86C1;">قائمة المقالات الجديدة المضافة:</h3>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%; text-align: center;">
          <tr style="background-color: #f2f2f2;">
            <th>عنوان المقال</th>
            <th>عدد الكلمات</th>
            <th>رابط فتح المستند</th>
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
    server.sendmail(sender, recipients, msg.as_string())
    server.quit()

def execute_article_transfer(coordinator_folder_id, month_folder_id):
    """Transfers articles into the designated month folder and updates tracking."""
    gc, drive_service, docs_service = get_google_services()
    sheet_id = st.secrets["SHEET_ID"]

    # 1. Fetch source files from coordinator folder
    query_source = f"'{coordinator_folder_id}' in parents and trashed = false and mimeType != 'application/vnd.google-apps.folder'"
    source_files_res = drive_service.files().list(
        q=query_source,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    source_files = source_files_res.get('files', [])

    if not source_files:
        return None, "لا توجد ملفات داخل مجلد المنسق."

    # 2. Check files already inside destination folder to prevent duplicates
    query_dest = f"'{month_folder_id}' in parents and trashed = false"
    dest_files_res = drive_service.files().list(
        q=query_dest,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    existing_dest_names = {f['name'].strip().lower() for f in dest_files_res.get('files', [])}

    # 3. Read Sheet tracking state
    sheet = gc.open_by_key(sheet_id).worksheet("Translation_Tracker")
    existing_sheet_data = sheet.get_all_values()
    table_data = existing_sheet_data if existing_sheet_data else [["عنوان المقال", "عدد الكلمات", "رابط المستند"]]

    processed_count = 0

    # 4. Copy each file directly into the designated month folder
    for file in source_files:
        file_name = file['name']
        if file_name.strip().lower() in existing_dest_names:
            continue

        copy_meta = {
            'name': file_name,
            'parents': [month_folder_id]
        }
        copy_res = drive_service.files().copy(
            fileId=file['id'],
            body=copy_meta,
            fields="id",
            supportsAllDrives=True
        ).execute()

        doc_id = copy_res['id']
        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        word_count = count_words(docs_service, doc_id)

        table_data.append([file_name, word_count, doc_url])
        existing_dest_names.add(file_name.strip().lower())
        processed_count += 1

    # 5. Save updates and send emails
    if processed_count > 0:
        sheet.clear()
        sheet.update(range_name='A1', values=table_data)
        try:
            send_notification_email(table_data)
        except Exception as e:
            return table_data, f"تم نسخ {processed_count} ملف بنجاح، لكن تعذر إرسال الإيميل: {e}"
        return table_data, f"تم بنجاح نقل ونسخ {processed_count} مقال إلى المجلد، وتحديث جدول التتبع والإيميل."
    else:
        return None, "جميع الملفات الموجودة في رابط المنسق موجودة مسبقاً داخل هذا المجلد."

# ==========================================
# Streamlit UI with Interactive State
# ==========================================
st.set_page_config(page_title="بوابة استلام المقالات", page_icon="📂", layout="centered")
st.title("بوابة استلام مقالات الترجمة")

if "folder_action" not in st.session_state:
    st.session_state.folder_action = None

coordinator_url = st.text_input("رابط مجلد المنسق (Google Drive):")

col1, col2 = st.columns(2)
with col1:
    months = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]
    selected_month_name = st.selectbox("شهر الإصدار:", months, index=datetime.datetime.now().month - 1)
    selected_month_num = months.index(selected_month_name) + 1

with col2:
    current_year = datetime.datetime.now().year
    selected_year = st.selectbox("سنة الإصدار:", range(current_year - 1, current_year + 5), index=1)

# Main trigger: Pre-flight inspection
if st.button("فحص واستيراد المقالات"):
    if not coordinator_url:
        st.warning("يرجى إدخال رابط المنسق أولاً.")
    else:
        source_folder_id = extract_folder_id(coordinator_url)
        if not source_folder_id:
            st.error("الرابط غير صحيح. تأكد من إدخال رابط مجلد Google Drive صالح.")
        else:
            with st.spinner("جاري فحص المجلدات في Google Drive..."):
                try:
                    gc, drive_service, docs_service = get_google_services()
                    root_folder_id = st.secrets["ROOT_TRANSLATION_FOLDER_ID"]
                    
                    inspection = detect_existing_folders(drive_service, root_folder_id, selected_year, selected_month_num)
                    
                    # Store inspection findings in session state
                    st.session_state.folder_action = {
                        "source_folder_id": source_folder_id,
                        "selected_year": selected_year,
                        "selected_month_num": selected_month_num,
                        "selected_month_name": selected_month_name,
                        "inspection": inspection
                    }
                except Exception as e:
                    st.error(f"خطأ أثناء فحص المجلدات: {e}")

# Confirmation Dialog Block (Shown if a folder exists or needs confirmation)
if st.session_state.folder_action:
    action_data = st.session_state.folder_action
    insp = action_data["inspection"]
    source_id = action_data["source_folder_id"]
    year = action_data["selected_year"]
    month_num = action_data["selected_month_num"]
    month_name = action_data["selected_month_name"]

    st.markdown("---")
    
    if insp["month_exists"]:
        found_name = insp["month_folder"]["name"]
        year_name = insp["year_folder"]["name"]
        
        st.warning(f"⚠️ **تنبيه:** تم العثور على مجلد مطابق/مشابه بالفعل:\n\n- المجلد: **`{found_name}`**\n- داخل: **`{year_name}`**")
        st.info("هل تود الاستمرار واستيراد المقالات إلى هذا المجلد الموجود مسبقاً؟")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ نعم، استمر واعتمد هذا المجلد"):
                with st.spinner("جاري نسخ الملفات وتحديث النظام..."):
                    try:
                        data, msg = execute_article_transfer(source_id, insp["month_folder"]["id"])
                        st.session_state.folder_action = None
                        if data:
                            st.success(msg)
                            st.balloons()
                        else:
                            st.info(msg)
                    except Exception as e:
                        st.error(f"حدث خطأ أثناء النقل: {e}")

        with c2:
            if st.button("❌ إلغاء العملية"):
                st.session_state.folder_action = None
                st.rerun()

    else:
        # Case: Month folder does not exist
        st.info(f"لم يتم العثور على مجلد سابق لشهر **{month_name} {year}**.")
        st.write(f"سيتم إنشاء مجلد جديد باسم: **`{month_num:02d} - {month_name} {year}`**")
        
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ تأكيد إنشاء المجلد وبدء النقل"):
                with st.spinner("جاري إنشاء المجلد ونقل المقالات..."):
                    try:
                        gc, drive_service, docs_service = get_google_services()
                        root_id = st.secrets["ROOT_TRANSLATION_FOLDER_ID"]
                        
                        # Resolve year folder ID or create it
                        if insp["year_exists"]:
                            year_id = insp["year_folder"]["id"]
                        else:
                            year_id = get_or_create_folder(drive_service, root_id, f"{year} Edition")

                        # Create the new standard month folder
                        target_month_name = f"{month_num:02d} - {month_name} {year}"
                        new_month_id = get_or_create_folder(drive_service, year_id, target_month_name)

                        # Execute transfer
                        data, msg = execute_article_transfer(source_id, new_month_id)
                        st.session_state.folder_action = None
                        if data:
                            st.success(msg)
                            st.balloons()
                        else:
                            st.info(msg)
                    except Exception as e:
                        st.error(f"حدث خطأ: {e}")

        with c2:
            if st.button("❌ إلغاء"):
                st.session_state.folder_action = None
                st.rerun()
