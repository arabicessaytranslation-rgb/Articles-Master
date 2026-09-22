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

# ==========================================
# 1. API Configuration & Auth
# ==========================================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly"
]

TEAM_RECIPIENTS = [
    "arabicessaytranslation@gmail.com",
    "ameermam.sa@gmail.com",
    "mohammedd9644@gmail.com",
    "keepcomingback.29@gmail.com",
    "ahmad2075533@gmail.com"
]

def get_google_services():
    """Initializes Google API clients using your stored OAuth token."""
    creds_dict = json.loads(st.secrets["gcp_oauth_token"])
    creds = Credentials.from_authorized_user_info(creds_dict, SCOPES)
    
    gc = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    docs_service = build('docs', 'v1', credentials=creds)
    
    return gc, drive_service, docs_service

def extract_folder_id(url):
    """Extracts Drive folder ID from a URL."""
    match = re.search(r'folders/([a-zA-Z0-9_-]+)', url)
    return match.group(1) if match else None

# ==========================================
# 2. Text Cleaning & Similarity Logic
# ==========================================
def strip_copy_prefix(name):
    """
    Strips 'Copy of', 'Copy (1) of', 'نسخة من' while strictly 
    preserving article numbers (e.g. 'Copy of 6. Article' -> '6. Article').
    """
    pattern = r'^(?:(?:copy\b(?:\s*\(\d+\)|\s+\d+)?\s+of\s*)|(?:نسخة\b(?:\s*\(\d+\)|\s+\d+)?\s+من\s*)|(?:copy\s*[:\-])\s*)+'
    cleaned = re.sub(pattern, '', name, flags=re.IGNORECASE).strip()
    return cleaned

def clean_article_title(raw_title):
    """Normalizes titles for comparison by stripping extensions, copy-prefixes, and symbols."""
    title = strip_copy_prefix(raw_title)
    title = re.sub(r'\.(docx|doc|gdoc|pdf)$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'[^a-zA-Z0-9\u0600-\u06FF\s]', ' ', title)
    return re.sub(r'\s+', ' ', title).strip().lower()

def extract_article_number(title):
    """Extracts leading or isolated article numbers for natural sorting."""
    match = re.search(r'^\s*(\d+)\s*[\.\-_]', title)
    if match:
        return int(match.group(1))
    nums = re.findall(r'\b\d+\b', title)
    return int(nums[0]) if nums else 9999

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

# ==========================================
# 3. Google Drive & Docs Helpers
# ==========================================
def list_subfolders(drive_service, parent_id):
    """Lists subfolders inside parent folder."""
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

def get_or_create_folder(drive_service, parent_id, folder_name):
    """Creates a new folder under parent."""
    metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    created = drive_service.files().create(body=metadata, fields='id', supportsAllDrives=True).execute()
    return created.get('id')

def count_words(docs_service, document_id):
    """Counts words directly from the Google Doc body."""
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

def compare_file_lists(source_files, dest_files, similarity_threshold=0.75):
    """Compares source files against destination files to isolate duplicates vs new items."""
    similar_pairs = []
    new_files = []

    for s_file in source_files:
        clean_s_name = strip_copy_prefix(s_file['name'])
        best_dest_match = None
        max_sim = 0.0

        for d_file in dest_files:
            clean_d_name = strip_copy_prefix(d_file['name'])
            sim = compute_title_similarity(clean_s_name, clean_d_name)
            if sim > max_sim:
                max_sim = sim
                best_dest_match = d_file

        if max_sim >= similarity_threshold and best_dest_match:
            similar_pairs.append({
                "source_id": s_file['id'],
                "source_name": s_file['name'],
                "clean_name": clean_s_name,
                "dest_name": best_dest_match['name'],
                "similarity": round(max_sim * 100, 1)
            })
        else:
            new_files.append({
                "id": s_file['id'],
                "name": s_file['name'],
                "clean_name": clean_s_name
            })

    return similar_pairs, new_files

# ==========================================
# 4. Google Sheets & Email Handlers
# ==========================================
def sync_to_translation_tracker(gc, sheet_id, year, month_name, month_num, new_records):
    """
    Safely logs records to Google Sheets:
    - Never clears headers or existing rows.
    - Uses File ID (Col G) as primary key to prevent duplication.
    - Injects interactive clickable =HYPERLINK() formulas.
    - Preserves manual columns for Translator and Status.
    """
    sheet = gc.open_by_key(sheet_id).worksheet("Translation_Tracker")
    existing_rows = sheet.get_all_values()
    
    headers = [
        "السنة", "الشهر", "رقم المقال", "عنوان المقال", 
        "عدد الكلمات", "المستند", "File ID", "المترجم", "الحالة"
    ]
    
    if not existing_rows or len(existing_rows) == 0:
        sheet.update(range_name='A1:I1', values=[headers])
        existing_rows = [headers]
        existing_file_ids = set()
    else:
        existing_file_ids = {row[6] for row in existing_rows[1:] if len(row) > 6}

    records_to_add = [r for r in new_records if r["file_id"] not in existing_file_ids]
    if not records_to_add:
        return 0

    # Natural numeric sorting
    records_to_add.sort(key=lambda x: extract_article_number(x["clean_name"]))

    month_label = f"{month_num:02d} - {month_name}"
    rows_payload = []
    for r in records_to_add:
        article_num = extract_article_number(r["clean_name"])
        article_num_str = str(article_num) if article_num != 9999 else "-"
        hyperlink_formula = f'=HYPERLINK("{r["doc_url"]}", "افتح المستند")'
        
        rows_payload.append([
            str(year),
            month_label,
            article_num_str,
            r["clean_name"],
            r["word_count"],
            hyperlink_formula,
            r["file_id"],
            "",                 # Column H: Translator (manual)
            "قيد الترجمة"       # Column I: Default Status
        ])

    sheet.append_rows(rows_payload, value_input_option='USER_ENTERED')
    return len(rows_payload)

def send_notification_email(table_data):
    """Sends HTML notification email with direct links."""
    sender = st.secrets["sender_email"]
    password = st.secrets["app_password"]

    msg = MIMEMultipart("alternative")
    msg['Subject'] = "تحديث: مقالات جديدة جاهزة للعمل المشترك"
    msg['From'] = sender
    msg['To'] = ", ".join(TEAM_RECIPIENTS)

    html = """
    <html dir="rtl">
      <body style="font-family: Arial, sans-serif;">
        <h3 style="color: #2E86C1;">قائمة المقالات الجديدة المضافة:</h3>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%; text-align: center;">
          <tr style="background-color: #f2f2f2;">
            <th>عنوان المقال</th>
            <th>عدد الكلمات</th>
            <th>رابط المستند</th>
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
    server.sendmail(sender, TEAM_RECIPIENTS, msg.as_string())
    server.quit()

# ==========================================
# 5. Core Execution Engine
# ==========================================
def execute_article_transfer(files_to_transfer, destination_folder_id, year=None, month_name=None, month_num=None, update_sheet_and_email=True):
    """Copies files into destination with cleaned names, updates Sheets, and alerts team."""
    gc, drive_service, docs_service = get_google_services()
    sheet_id = st.secrets["SHEET_ID"]
    
    new_records = []
    email_table_data = [["عنوان المقال", "عدد الكلمات", "رابط المستند"]]

    for file_info in files_to_transfer:
        raw_name = file_info['name']
        final_clean_name = strip_copy_prefix(raw_name)

        copy_meta = {
            'name': final_clean_name,
            'parents': [destination_folder_id]
        }
        copy_res = drive_service.files().copy(
            fileId=file_info['id'],
            body=copy_meta,
            fields="id",
            supportsAllDrives=True
        ).execute()

        doc_id = copy_res['id']
        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        word_count = count_words(docs_service, doc_id)

        new_records.append({
            "clean_name": final_clean_name,
            "word_count": word_count,
            "doc_url": doc_url,
            "file_id": doc_id
        })
        email_table_data.append([final_clean_name, word_count, doc_url])

    if update_sheet_and_email and new_records:
        sync_to_translation_tracker(gc, sheet_id, year, month_name, month_num, new_records)
        try:
            send_notification_email(email_table_data)
        except Exception as e:
            return new_records, f"تم نسخ {len(new_records)} مقال وتحديث الجدول، لكن فشل إرسال الإيميل: {e}"

    return new_records, f"تم بنجاح نسخ {len(new_records)} مقال مع تنظيف الأسماء وتحديث المنظومة بالكامل."

# ==========================================
# 6. Streamlit User Interface
# ==========================================
st.set_page_config(page_title="منظومة إدارة ونسخ المقالات", page_icon="📑", layout="wide")
st.title("منظومة إدارة ونسخ مقالات الترجمة")

tab1, tab2 = st.tabs(["📥 معالجة وإصدار المقالات الشهرية", "📂 نسخ مجلد إلى مستودع خارجي"])

# --- TAB 1: Monthly Workflow ---
with tab1:
    st.subheader("استلام وتوزيع مقالات الإصدار الشهري")

    if "inspection_data_tab1" not in st.session_state:
        st.session_state.inspection_data_tab1 = None

    coordinator_url = st.text_input("رابط مجلد المنسق (Google Drive):", key="coord_url")

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

    if st.button("فحص المجلد والمحتوى الشهري", key="btn_check_tab1"):
        if not coordinator_url:
            st.warning("يرجى إدخال رابط المنسق أولاً.")
        else:
            source_folder_id = extract_folder_id(coordinator_url)
            if not source_folder_id:
                st.error("الرابط غير صحيح.")
            else:
                with st.spinner("جاري فحص المجلدات ومطابقة العناوين..."):
                    try:
                        gc, drive_service, docs_service = get_google_services()
                        root_id = st.secrets["ROOT_TRANSLATION_FOLDER_ID"]
                        
                        source_files = list_files_in_folder(drive_service, source_folder_id)
                        year_folders = list_subfolders(drive_service, root_id)
                        matched_year = next((f for f in year_folders if str(selected_year) in f['name']), None)

                        matched_month = None
                        dest_files = []
                        if matched_year:
                            m_folders = list_subfolders(drive_service, matched_year['id'])
                            t_num = str(selected_month_num)
                            t_pad = f"{selected_month_num:02d}"
                            for f in m_folders:
                                f_lower = f['name'].lower()
                                nums = re.findall(r'\b\d{1,2}\b', f['name'])
                                if selected_month_name.lower() in f_lower or t_num in nums or t_pad in nums:
                                    matched_month = f
                                    dest_files = list_files_in_folder(drive_service, f['id'])
                                    break

                        similar_pairs, new_files = compare_file_lists(source_files, dest_files)

                        st.session_state.inspection_data_tab1 = {
                            "source_files": source_files,
                            "similar_pairs": similar_pairs,
                            "new_files": new_files,
                            "year_folder": matched_year,
                            "month_folder": matched_month,
                            "selected_year": selected_year,
                            "selected_month_num": selected_month_num,
                            "selected_month_name": selected_month_name
                        }
                    except Exception as e:
                        st.error(f"خطأ أثناء الفحص: {e}")

    # Decision Block Tab 1
    if st.session_state.inspection_data_tab1:
        st.markdown("---")
        info = st.session_state.inspection_data_tab1
        similar_pairs = info["similar_pairs"]
        new_files = info["new_files"]
        source_files = info["source_files"]
        m_folder = info["month_folder"]

        if not source_files:
            st.warning("مجلد المنسق فارغ، لا توجد ملفات.")
        else:
            if m_folder:
                st.warning(f"📁 تم رصد مجلد موجود مسبقاً لهذا الشهر: **`{m_folder['name']}`**")
            else:
                st.info(f"💡 سيتم إنشاء مجلد جديد باسم: **`{info['selected_month_num']:02d} - {info['selected_month_name']} {info['selected_year']}`**")

            if similar_pairs:
                st.error(f"⚠️ تم رصد مقالات مشابهة/مكررة ({len(similar_pairs)} من أصل {len(source_files)}):")
                st.dataframe(pd.DataFrame([{
                    "اسم الملف الأصلي": p["source_name"],
                    "الاسم بعد التنظيف": p["clean_name"],
                    "الملف المقابل بالمستودع": p["dest_name"],
                    "التطابق": f"{p['similarity']}%"
                } for p in similar_pairs]), use_container_width=True)

            c1, c2, c3 = st.columns(3)
            with c1:
                if new_files and st.button(f"✅ نسخ المقالات الجديدة فقط ({len(new_files)})", key="copy_new_tab1"):
                    with st.spinner("جاري نسخ الجديد وتحديث النظام..."):
                        gc, drive_service, docs_service = get_google_services()
                        root_id = st.secrets["ROOT_TRANSLATION_FOLDER_ID"]
                        
                        target_m_id = m_folder['id'] if m_folder else get_or_create_folder(
                            drive_service, 
                            info['year_folder']['id'] if info['year_folder'] else get_or_create_folder(drive_service, root_id, f"{info['selected_year']} Edition"),
                            f"{info['selected_month_num']:02d} - {info['selected_month_name']} {info['selected_year']}"
                        )
                        _, msg = execute_article_transfer(
                            new_files, target_m_id, 
                            year=info['selected_year'], 
                            month_name=info['selected_month_name'], 
                            month_num=info['selected_month_num'], 
                            update_sheet_and_email=True
                        )
                        st.session_state.inspection_data_tab1 = None
                        st.success(msg)
                        st.balloons()

            with c2:
                if st.button("⚡ نسخ جميع الملفات وتجاوز التشابه", key="copy_all_tab1"):
                    with st.spinner("جاري نسخ كافة الملفات..."):
                        gc, drive_service, docs_service = get_google_services()
                        root_id = st.secrets["ROOT_TRANSLATION_FOLDER_ID"]
                        target_m_id = m_folder['id'] if m_folder else get_or_create_folder(
                            drive_service, 
                            info['year_folder']['id'] if info['year_folder'] else get_or_create_folder(drive_service, root_id, f"{info['selected_year']} Edition"),
                            f"{info['selected_month_num']:02d} - {info['selected_month_name']} {info['selected_year']}"
                        )
                        _, msg = execute_article_transfer(
                            source_files, target_m_id, 
                            year=info['selected_year'], 
                            month_name=info['selected_month_name'], 
                            month_num=info['selected_month_num'], 
                            update_sheet_and_email=True
                        )
                        st.session_state.inspection_data_tab1 = None
                        st.success(msg)
                        st.balloons()

            with c3:
                if st.button("❌ إلغاء العملية", key="cancel_tab1"):
                    st.session_state.inspection_data_tab1 = None
                    st.rerun()

# --- TAB 2: Direct Folder Cloner ---
with tab2:
    st.subheader("نسخ محتوى مجلد كامل إلى مستودع خارجي مع الفحص الذكي")
    st.caption("يقوم هذا القسم بنسخ الملفات بين أي مجلدين تمتلك صلاحية Editor عليهما، مع إزالة 'Copy of'، وحفظ تسلسل الأرقام، وتفادي تكرار المتشابهات.")

    if "inspection_data_tab2" not in st.session_state:
        st.session_state.inspection_data_tab2 = None

    col_src, col_dst = st.columns(2)
    with col_src:
        src_url = st.text_input("رابط المجلد المصدر (Source Folder):", key="tab2_src")
    with col_dst:
        dst_url = st.text_input("رابط المستودع الهدف (Destination Folder):", key="tab2_dst")

    if st.button("فحص ومقارنة المجلدين", key="btn_check_tab2"):
        if not src_url or not dst_url:
            st.warning("يرجى إدخال رابط المجلد المصدر ورابط المجلد الهدف.")
        else:
            src_id = extract_folder_id(src_url)
            dst_id = extract_folder_id(dst_url)
            if not src_id or not dst_id:
                st.error("أحد الروابط غير صحيح.")
            else:
                with st.spinner("جاري فحص المجلدين وقراءة الملفات..."):
                    try:
                        gc, drive_service, docs_service = get_google_services()
                        src_files = list_files_in_folder(drive_service, src_id)
                        dst_files = list_files_in_folder(drive_service, dst_id)

                        similar_pairs, new_files = compare_file_lists(src_files, dst_files)

                        st.session_state.inspection_data_tab2 = {
                            "src_id": src_id,
                            "dst_id": dst_id,
                            "src_files": src_files,
                            "dst_files": dst_files,
                            "similar_pairs": similar_pairs,
                            "new_files": new_files
                        }
                    except Exception as e:
                        st.error(f"حدث خطأ أثناء فحص المجلدات: {e}")

    # Decision Block Tab 2
    if st.session_state.inspection_data_tab2:
        st.markdown("---")
        info2 = st.session_state.inspection_data_tab2
        similar_pairs = info2["similar_pairs"]
        new_files = info2["new_files"]
        src_files = info2["src_files"]
        dst_id = info2["dst_id"]

        if not src_files:
            st.warning("المجلد المصدر لا يحتوي على أي ملفات.")
        else:
            st.write(f"📊 **إجمالي ملفات المصدر:** {len(src_files)} | **الملفات الموجودة بالهدف:** {len(info2['dst_files'])}")

            if similar_pairs:
                st.error(f"⚠️ تم رصد ({len(similar_pairs)}) ملفات موجودة أو مشابهة مسبقاً بالمستودع الهدف:")
                st.dataframe(pd.DataFrame([{
                    "اسم الملف بالمصدر": p["source_name"],
                    "الاسم بعد إزالة Copy of": p["clean_name"],
                    "الملف المقابل بالهدف": p["dest_name"],
                    "التطابق": f"{p['similarity']}%"
                } for p in similar_pairs]), use_container_width=True)

            b1, b2, b3 = st.columns(3)
            with b1:
                if new_files and st.button(f"✅ نسخ الملفات الجديدة فقط ({len(new_files)})", key="copy_new_tab2"):
                    with st.spinner("جاري النسخ النظيف للملفات الجديدة..."):
                        _, msg = execute_article_transfer(new_files, dst_id, update_sheet_and_email=False)
                        st.session_state.inspection_data_tab2 = None
                        st.success(msg)
                        st.balloons()

            with b2:
                if st.button(f"⚡ نسخ كافة الملفات ({len(src_files)}) وتجاوز التشابه", key="copy_all_tab2"):
                    with st.spinner("جاري نسخ جميع الملفات..."):
                        _, msg = execute_article_transfer(src_files, dst_id, update_sheet_and_email=False)
                        st.session_state.inspection_data_tab2 = None
                        st.success(msg)
                        st.balloons()

            with b3:
                if st.button("❌ إلغاء العملية", key="cancel_tab2"):
                    st.session_state.inspection_data_tab2 = None
                    st.rerun()
