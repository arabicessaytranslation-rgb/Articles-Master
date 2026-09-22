import streamlit as st
import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re
import json
from difflib import SequenceMatcher
import pandas as pd
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
    """Cleans text for folder name matching."""
    val = re.sub(r'[^a-zA-Z0-9\u0600-\u06FF\s]', ' ', val)
    return re.sub(r'\s+', ' ', val).strip().lower()

def clean_article_title(raw_title):
    """Normalizes article filenames for fuzzy title comparison."""
    # Strip common file extensions
    title = re.sub(r'\.(docx|doc|gdoc|pdf)$', '', raw_title, flags=re.IGNORECASE)
    # Remove symbols and punctuation while preserving Arabic, English, and numbers
    title = re.sub(r'[^a-zA-Z0-9\u0600-\u06FF\s]', ' ', title)
    return re.sub(r'\s+', ' ', title).strip().lower()

def compute_title_similarity(t1, t2):
    """Calculates similarity score (0.0 to 1.0) between two article titles."""
    c1 = clean_article_title(t1)
    c2 = clean_article_title(t2)
    if not c1 or not c2:
        return 0.0
    if c1 == c2:
        return 1.0
    if c1 in c2 or c2 in c1:
        return max(0.85, SequenceMatcher(None, c1, c2).ratio())
    return SequenceMatcher(None, c1, c2).ratio()

def list_subfolders(drive_service, parent_id):
    """Lists non-trashed subfolders inside a parent folder."""
    query = f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    response = drive_service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        pageSize=100
    ).execute()
    return response.get('files', [])

def list_files_in_folder(drive_service, folder_id):
    """Lists non-folder files inside a specific folder."""
    query = f"'{folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
    response = drive_service.files().list(
        q=query,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        pageSize=100
    ).execute()
    return response.get('files', [])

def inspect_repository_and_content(drive_service, root_folder_id, source_folder_id, target_year, target_month_num, similarity_threshold=0.75):
    """
    Performs full pre-flight scan:
    1. Detects Year and Month folders.
    2. Scans files in source vs destination.
    3. Detects similar articles by fuzzy matching titles.
    """
    # 1. Fetch Source Files
    source_files = list_files_in_folder(drive_service, source_folder_id)

    # 2. Inspect Year Folder
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

    # 3. Inspect Month Folder
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

    # 4. Compare Content Similarities if Month Folder Exists
    dest_files = []
    similar_pairs = []
    new_files = []

    if matched_month:
        dest_files = list_files_in_folder(drive_service, matched_month['id'])
        
        for s_file in source_files:
            s_name = s_file['name']
            best_dest_match = None
            max_sim = 0.0

            for d_file in dest_files:
                d_name = d_file['name']
                sim = compute_title_similarity(s_name, d_name)
                if sim > max_sim:
                    max_sim = sim
                    best_dest_match = d_file

            if max_sim >= similarity_threshold and best_dest_match:
                similar_pairs.append({
                    "source_id": s_file['id'],
                    "source_name": s_name,
                    "dest_name": best_dest_match['name'],
                    "similarity": round(max_sim * 100, 1)
                })
            else:
                new_files.append(s_file)
    else:
        new_files = source_files

    return {
        "source_files": source_files,
        "year_exists": matched_year is not None,
        "year_folder": matched_year,
        "month_exists": matched_month is not None,
        "month_folder": matched_month,
        "existing_dest_files_count": len(dest_files),
        "similar_pairs": similar_pairs,
        "new_files": new_files
    }

def get_or_create_folder(drive_service, parent_id, folder_name):
    """Creates folder under parent if needed."""
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

def execute_article_transfer(files_to_transfer, month_folder_id):
    """Copies specified list of files directly into the destination month folder."""
    gc, drive_service, docs_service = get_google_services()
    sheet_id = st.secrets["SHEET_ID"]

    if not files_to_transfer:
        return None, "لا توجد ملفات محددة للنقل."

    # Read current Google Sheet data
    sheet = gc.open_by_key(sheet_id).worksheet("Translation_Tracker")
    existing_sheet_data = sheet.get_all_values()
    table_data = existing_sheet_data if existing_sheet_data else [["عنوان المقال", "عدد الكلمات", "رابط المستند"]]

    processed_count = 0

    for file in files_to_transfer:
        file_name = file['name']
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
        processed_count += 1

    if processed_count > 0:
        sheet.clear()
        sheet.update(range_name='A1', values=table_data)
        try:
            send_notification_email(table_data)
        except Exception as e:
            return table_data, f"تم نسخ {processed_count} مقال بنجاح، لكن تعذر إرسال الإيميل: {e}"
        return table_data, f"تم بنجاح نسخ {processed_count} مقال إلى المجلد وتحديث جدول المتابعة وإرسال الإيميل."
    else:
        return None, "لم يتم نسخ أي ملفات جديدة."

# ==========================================
# Streamlit UI with Dual Verification
# ==========================================
st.set_page_config(page_title="بوابة استلام المقالات", page_icon="📂", layout="centered")
st.title("بوابة استلام مقالات الترجمة")

if "inspection_data" not in st.session_state:
    st.session_state.inspection_data = None

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

# Step 1: Pre-Flight Scan & Content Inspection
if st.button("فحص المجلد والمحتوى"):
    if not coordinator_url:
        st.warning("يرجى إدخال رابط المنسق أولاً.")
    else:
        source_folder_id = extract_folder_id(coordinator_url)
        if not source_folder_id:
            st.error("الرابط غير صحيح. يرجى التأكد من إدخال رابط مجلد صالح.")
        else:
            with st.spinner("جاري فحص المجلد ومقارنة تشابه أسماء المقالات..."):
                try:
                    gc, drive_service, docs_service = get_google_services()
                    root_folder_id = st.secrets["ROOT_TRANSLATION_FOLDER_ID"]
                    
                    inspection = inspect_repository_and_content(
                        drive_service, root_folder_id, source_folder_id, selected_year, selected_month_num
                    )
                    
                    st.session_state.inspection_data = {
                        "source_folder_id": source_folder_id,
                        "selected_year": selected_year,
                        "selected_month_num": selected_month_num,
                        "selected_month_name": selected_month_name,
                        "inspection": inspection
                    }
                except Exception as e:
                    st.error(f"حدث خطأ أثناء فحص البيانات: {e}")

# Step 2: Interactive Decision Card (Approval Flow)
if st.session_state.inspection_data:
    st.markdown("---")
    act = st.session_state.inspection_data
    insp = act["inspection"]
    source_files = insp["source_files"]
    similar_pairs = insp["similar_pairs"]
    new_files = insp["new_files"]
    year = act["selected_year"]
    month_name = act["selected_month_name"]
    month_num = act["selected_month_num"]

    if not source_files:
        st.warning("مجلد المنسق فارغ، لا توجد ملفات لنقلها.")
        if st.button("إغلاق"):
            st.session_state.inspection_data = None
            st.rerun()

    elif insp["month_exists"]:
        found_month_name = insp["month_folder"]["name"]
        found_year_name = insp["year_folder"]["name"]
        
        st.warning(f"📁 **تنبيه:** تم العثور على مجلد مطابق: **`{found_month_name}`** داخل **`{found_year_name}`**")

        # Check for title similarities
        if similar_pairs:
            st.error(f"⚠️ **تم اكتشاف مقالات مشابهة/موجودة مسبقاً ({len(similar_pairs)} من أصل {len(source_files)} ملفات):**")
            
            # Display comparison table
            table_records = []
            for p in similar_pairs:
                table_records.append({
                    "ملف المنسق (الجديد)": p["source_name"],
                    "الملف الموجود المشابه": p["dest_name"],
                    "نسبة التطابق": f"{p['similarity']}%"
                })
            st.dataframe(pd.DataFrame(table_records), use_container_width=True)

            if new_files:
                st.info(f"💡 توجد أيضاً **{len(new_files)}** مقالات جديدة تماماً لا يوجد لها تشابه.")
            else:
                st.warning("⚠️ جميع المقالات في مجلد المنسق مشابهة جداً لمقالات موجودة مسبقاً في هذا المجلد.")

            # Decision Buttons for Similarities
            st.write("### اختر الإجراء المطلوب:")
            c1, c2, c3 = st.columns([1.5, 1.2, 0.8])
            
            with c1:
                if new_files:
                    if st.button(f"✅ نسخ المقالات الجديدة فقط ({len(new_files)}) وتخطي المتشابهة"):
                        with st.spinner("جاري استيراد المقالات الجديدة فقط..."):
                            data, msg = execute_article_transfer(new_files, insp["month_folder"]["id"])
                            st.session_state.inspection_data = None
                            if data:
                                st.success(msg)
                                st.balloons()
                            else:
                                st.info(msg)
                else:
                    st.button("✅ نسخ المقالات الجديدة فقط", disabled=True)

            with c2:
                if st.button("⚡ استيراد الكل وتجاهل التشابه"):
                    with st.spinner("جاري استيراد جميع الملفات..."):
                        data, msg = execute_article_transfer(source_files, insp["month_folder"]["id"])
                        st.session_state.inspection_data = None
                        if data:
                            st.success(msg)
                            st.balloons()
                        else:
                            st.info(msg)

            with c3:
                if st.button("❌ إلغاء العملية"):
                    st.session_state.inspection_data = None
                    st.rerun()

        else:
            # Folder exists, but all files inside are completely different/new
            st.success(f"المجلد موجود مسبقاً، ولكن جميع المقالات في رابط المنسق ({len(source_files)} مقال) جديدة تماماً ولا يوجد أي تشابه في العناوين.")
            st.info("هل تود اعتماد هذا المجلد ونسخ الملفات إليه؟")
            
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ نعم، استمر واعتمد المجلد"):
                    with st.spinner("جاري نسخ المقالات..."):
                        data, msg = execute_article_transfer(source_files, insp["month_folder"]["id"])
                        st.session_state.inspection_data = None
                        if data:
                            st.success(msg)
                            st.balloons()
                        else:
                            st.info(msg)
            with c2:
                if st.button("❌ إلغاء"):
                    st.session_state.inspection_data = None
                    st.rerun()

    else:
        # Case: Brand new month folder
        st.info(f"لم يتم العثور على مجلد سابق لشهر **{month_name} {year}**.")
        st.write(f"سيتم إنشاء مجلد جديد باسم: **`{month_num:02d} - {month_name} {year}`** ونقل **{len(source_files)}** مقال إليه.")
        
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ إنشاء المجلد وبدء النقل"):
                with st.spinner("جاري إنشاء المجلد ونقل المقالات..."):
                    gc, drive_service, docs_service = get_google_services()
                    root_id = st.secrets["ROOT_TRANSLATION_FOLDER_ID"]

                    if insp["year_exists"]:
                        year_id = insp["year_folder"]["id"]
                    else:
                        year_id = get_or_create_folder(drive_service, root_id, f"{year} Edition")

                    target_month_name = f"{month_num:02d} - {month_name} {year}"
                    new_month_id = get_or_create_folder(drive_service, year_id, target_month_name)

                    data, msg = execute_article_transfer(source_files, new_month_id)
                    st.session_state.inspection_data = None
                    if data:
                        st.success(msg)
                        st.balloons()
                    else:
                        st.info(msg)

        with c2:
            if st.button("❌ إلغاء"):
                st.session_state.inspection_data = None
                st.rerun()
