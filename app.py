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

# --- Scopes ---
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
    """Normalizes string for comparison: removes punctuation, trims extra whitespace, lowercases."""
    val = re.sub(r'[^a-zA-Z0-9\u0600-\u06FF\s]', ' ', val)
    return re.sub(r'\s+', ' ', val).strip().lower()

def list_subfolders(drive_service, parent_id):
    """Retrieves all non-trashed subfolders directly inside a parent folder."""
    query = f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    response = drive_service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        pageSize=100
    ).execute()
    return response.get('files', [])

def resolve_year_folder(drive_service, root_folder_id, target_year):
    """
    Finds existing year folder by numeric anchor (e.g., '2026', '2026 Edition', 'Edition 2026').
    Creates one standard folder if not found.
    """
    subfolders = list_subfolders(drive_service, root_folder_id)
    year_str = str(target_year)
    standard_name = f"{target_year} Edition"

    # Match by year number anchor
    for folder in subfolders:
        tokens = re.findall(r'\b\d{4}\b', folder['name'])
        if year_str in tokens or year_str in folder['name']:
            return folder['id']

    # Fallback to string similarity if no strict regex match
    for folder in subfolders:
        sim = SequenceMatcher(None, normalize_string(standard_name), normalize_string(folder['name'])).ratio()
        if sim >= 0.70:
            return folder['id']

    # Create standard folder if completely missing
    metadata = {
        'name': standard_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [root_folder_id]
    }
    created = drive_service.files().create(body=metadata, fields='id', supportsAllDrives=True).execute()
    return created.get('id')

def resolve_month_folder(drive_service, year_folder_id, target_month_num, target_year):
    """
    Finds existing month folder under the year folder.
    Matches against: full month name, 3-letter abbreviation, and 1- or 2-digit numeric representations.
    Creates a standardized folder if not found.
    """
    subfolders = list_subfolders(drive_service, year_folder_id)
    
    month_date = datetime.date(target_year, target_month_num, 1)
    full_name = month_date.strftime('%B')        # e.g., "October"
    short_name = month_date.strftime('%b')       # e.g., "Oct"
    standard_name = f"{target_month_num:02d} - {full_name} {target_year}"

    target_num_str = str(target_month_num)
    target_num_padded = f"{target_month_num:02d}"

    best_match_id = None
    highest_score = 0.0

    for folder in subfolders:
        name_clean = normalize_string(folder['name'])
        tokens = name_clean.split()

        # 1. Check direct name anchor (e.g., "october" or "oct")
        has_name_anchor = (full_name.lower() in name_clean) or (short_name.lower() in tokens)

        # 2. Check numeric anchor (e.g., isolated "10" or "09")
        folder_numbers = re.findall(r'\b\d{1,2}\b', folder['name'])
        has_number_anchor = (target_num_str in folder_numbers) or (target_num_padded in folder_numbers)

        if has_name_anchor or has_number_anchor:
            sim = SequenceMatcher(None, normalize_string(standard_name), name_clean).ratio()
            if sim > highest_score:
                highest_score = sim
                best_match_id = folder['id']

    if best_match_id and highest_score >= 0.35:
        return best_match_id

    # Create standard folder if completely missing
    metadata = {
        'name': standard_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [year_folder_id]
    }
    created = drive_service.files().create(body=metadata, fields='id', supportsAllDrives=True).execute()
    return created.get('id')

def count_words(docs_service, document_id):
    """Counts words directly from the Google Document body."""
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
    """Sends notification email to team recipients with direct document links."""
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

def process_articles(coordinator_folder_id, selected_month, selected_year):
    """Processes source folder files directly into the resolved year/month folders."""
    gc, drive_service, docs_service = get_google_services()
    root_folder_id = st.secrets["ROOT_TRANSLATION_FOLDER_ID"]
    sheet_id = st.secrets["SHEET_ID"]

    # 1. Fetch source files from coordinator's folder
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

    # 2. Resolve Year and Month folders (detects existing variations before creating)
    year_folder_id = resolve_year_folder(drive_service, root_folder_id, selected_year)
    month_folder_id = resolve_month_folder(drive_service, year_folder_id, selected_month, selected_year)

    # 3. Read files already inside month folder to avoid duplicates
    query_month_files = f"'{month_folder_id}' in parents and trashed = false"
    month_files_res = drive_service.files().list(
        q=query_month_files,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    existing_file_names = {f['name'].strip().lower() for f in month_files_res.get('files', [])}

    # 4. Read Sheet tracking state
    sheet = gc.open_by_key(sheet_id).worksheet("Translation_Tracker")
    existing_sheet_data = sheet.get_all_values()
    table_data = existing_sheet_data if existing_sheet_data else [["عنوان المقال", "عدد الكلمات", "رابط المستند"]]

    processed_count = 0

    # 5. Copy files directly into month folder
    for file in source_files:
        file_name = file['name']

        if file_name.strip().lower() in existing_file_names:
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
        existing_file_names.add(file_name.strip().lower())
        processed_count += 1

    # 6. Update Tracker Sheet and dispatch notifications
    if processed_count > 0:
        sheet.clear()
        sheet.update(range_name='A1', values=table_data)
        try:
            send_notification_email(table_data)
        except Exception as e:
            return table_data, f"تم نسخ {processed_count} ملف بنجاح، لكن تعذر إرسال الإيميل: {e}"
        return table_data, f"تم بنجاح نسخ {processed_count} مقال إلى مجلد الشهر وتحديث جدول المتابعة والإيميل."
    else:
        return None, "جميع الملفات الموجودة في رابط المنسق موجودة بالفعل داخل مجلد الشهر."

# ==========================================
# Streamlit UI
# ==========================================
st.set_page_config(page_title="بوابة استلام المقالات", page_icon="📂", layout="centered")
st.title("بوابة استلام مقالات الترجمة")

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

if st.button("سحب المقالات وبدء العمل"):
    if not coordinator_url:
        st.warning("يرجى إدخال رابط المنسق أولاً.")
    else:
        source_folder_id = extract_folder_id(coordinator_url)
        if not source_folder_id:
            st.error("الرابط غير صحيح. تأكد من إدخال رابط صالح لمجلد Google Drive يحتوي على 'folders/ID'.")
        else:
            with st.spinner("جاري فحص المجلدات واستيراد المقالات..."):
                try:
                    data, msg = process_articles(source_folder_id, selected_month_num, selected_year)
                    if data:
                        st.success(msg)
                        st.balloons()
                    else:
                        st.info(msg)
                except Exception as e:
                    st.error(f"حدث خطأ أثناء التشغيل: {e}")
