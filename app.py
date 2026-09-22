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

TRACKER_HEADERS = [
    "المترجم",
    "المدقق",
    "المسجل",
    "عنوان المقال",
    "رابط المقال",
    "السنة والشهر للعدد"
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

def get_folder_details(drive_service, folder_id):
    """جلب اسم ورابط المجلد مباشرة من جوجل درايف"""
    try:
        folder = drive_service.files().get(
            fileId=folder_id, 
            fields="name, webViewLink",
            supportsAllDrives=True
        ).execute()
        return folder.get('name', 'مجلد غير معروف'), folder.get('webViewLink', f"https://drive.google.com/drive/folders/{folder_id}")
    except Exception:
        return "مجلد العمل", f"https://drive.google.com/drive/folders/{folder_id}"

# ==========================================
# 2. Text Cleaning & Sorting Helpers
# ==========================================
def strip_copy_prefix(name):
    """إزالة بادئة 'Copy of' مع الحفاظ الصارم على ترقيم المقالات"""
    pattern = r'^(?:(?:copy\b(?:\s*\(\d+\)|\s+\d+)?\s+of\s*)|(?:نسخة\b(?:\s*\(\d+\)|\s+\d+)?\s+من\s*)|(?:copy\s*[:\-])\s*)+'
    return re.sub(pattern, '', name, flags=re.IGNORECASE).strip()

def clean_article_title(raw_title):
    """تنظيف العنوان للمقارنة الذكية"""
    title = strip_copy_prefix(raw_title)
    title = re.sub(r'\.(docx|doc|gdoc|pdf)$', '', title, flags=re.IGNORECASE)
    title = re.sub(r'[^a-zA-Z0-9\u0600-\u06FF\s]', ' ', title)
    return re.sub(r'\s+', ' ', title).strip().lower()

def extract_article_number(title):
    """استخراج رقم المقال لترتيب الأسطر رقمياً"""
    match = re.search(r'^\s*(\d+)\s*[\.\-_]', title)
    if match:
        return int(match.group(1))
    nums = re.findall(r'\b\d+\b', title)
    return int(nums[0]) if nums else 9999

def compute_title_similarity(t1, t2):
    """حساب نسبة تشابه العناوين"""
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
# 3. Google Drive Operations
# ==========================================
def list_subfolders(drive_service, parent_id):
    """جلب المجلدات الفرعية"""
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
    """جلب الملفات من المجلد"""
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
    """إنشاء مجلد في حال عدم وجوده"""
    metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    created = drive_service.files().create(body=metadata, fields='id', supportsAllDrives=True).execute()
    return created.get('id')

def count_words(docs_service, document_id):
    """حساب عدد الكلمات الحقيقي من المستند"""
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
    """مقارنة الملفات وفرز الجديد عن المتشابه"""
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
# 4. Sheets & Modern Email Operations
# ==========================================
def reset_and_populate_tracker(gc, sheet_id, year, month_name, month_num, records):
    """تفريغ ما تحت الرؤوس وإعادة تعبئة جدول المتابعة مرتباً برقم المقال"""
    sheet = gc.open_by_key(sheet_id).worksheet("Translation_Tracker")
    existing_rows = sheet.get_all_values()

    headers_match = False
    if existing_rows and len(existing_rows) > 0:
        first_row = [str(c).strip() for c in existing_rows[0]]
        if len(first_row) >= len(TRACKER_HEADERS) and first_row[:len(TRACKER_HEADERS)] == TRACKER_HEADERS:
            headers_match = True

    if not headers_match:
        sheet.update(range_name='A1:F1', values=[TRACKER_HEADERS], value_input_option='USER_ENTERED')
        try:
            sheet.freeze(rows=1)
        except Exception:
            pass

    try:
        sheet.batch_clear(["A2:Z"])
    except Exception:
        total_rows = len(existing_rows) if existing_rows else 100
        if total_rows > 1:
            sheet.batch_clear([f"A2:Z{total_rows}"])

    if not records:
        return 0

    sorted_records = sorted(records, key=lambda x: extract_article_number(x["clean_name"]))
    edition_label = f"{month_num:02d} - {month_name} {year}"
    rows_payload = []

    for r in sorted_records:
        hyperlink_formula = f'=HYPERLINK("{r["doc_url"]}", "افتح المقال")'
        rows_payload.append([
            "",
            "",
            "",
            r["clean_name"],
            hyperlink_formula,
            edition_label
        ])

    sheet.update(range_name=f'A2:F{len(rows_payload)+1}', values=rows_payload, value_input_option='USER_ENTERED')
    return len(rows_payload)

def send_monthly_notification_email(table_data, edition_label, sheet_id):
    """إرسال إيميل الإصدار الشهري بتصميم حديث وبطاقة فتح الشيت"""
    sender = st.secrets["sender_email"]
    password = st.secrets["app_password"]
    sheet_link = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"

    total_articles = len(table_data) - 1
    total_words = sum([r[1] for r in table_data[1:] if isinstance(r[1], int)])

    msg = MIMEMultipart("alternative")
    msg['Subject'] = f"📢 تحديث: مقالات عدد ({edition_label}) جاهزة للعمل"
    msg['From'] = sender
    msg['To'] = ", ".join(TEAM_RECIPIENTS)

    rows_html = ""
    for idx, row in enumerate(table_data[1:], 1):
        bg = "#f8fafc" if idx % 2 == 0 else "#ffffff"
        rows_html += f"""
        <tr style="background-color: {bg}; border-bottom: 1px solid #e2e8f0;">
          <td style="padding: 10px; text-align: center; color: #64748b; font-weight: bold;">{idx}</td>
          <td style="padding: 10px; text-align: right; color: #1e293b; font-weight: 500;">{row[0]}</td>
          <td style="padding: 10px; text-align: center; color: #0f172a; font-weight: bold;">{row[1]}</td>
          <td style="padding: 10px; text-align: center;">
            <a href="{row[2]}" style="background-color: #0284c7; color: #ffffff; padding: 6px 14px; text-decoration: none; border-radius: 4px; font-size: 13px; font-weight: bold;">افتح المقال</a>
          </td>
        </tr>
        """

    html = f"""
    <html dir="rtl">
      <body style="font-family: Arial, sans-serif; background-color: #f1f5f9; padding: 25px; margin: 0;">
        <div style="max-width: 680px; margin: auto; background-color: #ffffff; border-radius: 8px; border: 1px solid #cbd5e1; overflow: hidden;">
          <div style="background-color: #0f172a; padding: 20px; text-align: center; color: #ffffff;">
            <h2 style="margin: 0; font-size: 20px;">إصدار المقالات الجديد: {edition_label}</h2>
          </div>
          <div style="padding: 24px;">
            <div style="display: flex; gap: 10px; margin-bottom: 20px; text-align: center;">
              <div style="flex: 1; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px;">
                <span style="font-size: 13px; color: #64748b;">إجمالي المقالات</span>
                <div style="font-size: 20px; font-weight: bold; color: #0f172a;">{total_articles} مقالات</div>
              </div>
              <div style="flex: 1; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px;">
                <span style="font-size: 13px; color: #64748b;">مجموع الكلمات</span>
                <div style="font-size: 20px; font-weight: bold; color: #0284c7;">{total_words:,} كلمة</div>
              </div>
            </div>
            <div style="text-align: center; margin-bottom: 25px;">
              <a href="{sheet_link}" style="background-color: #16a34a; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 14px; display: inline-block;">📋 فتح جدول المتابعة لتسجيل وتوزيع المهام</a>
            </div>
            <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
              <thead>
                <tr style="background-color: #f1f5f9; color: #334155; border-bottom: 2px solid #cbd5e1;">
                  <th style="padding: 10px; text-align: center;">#</th>
                  <th style="padding: 10px; text-align: right;">عنوان المقال</th>
                  <th style="padding: 10px; text-align: center;">الكلمات</th>
                  <th style="padding: 10px; text-align: center;">المستند</th>
                </tr>
              </thead>
              <tbody>
                {rows_html}
              </tbody>
            </table>
          </div>
        </div>
      </body>
    </html>
    """
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
    server.login(sender, password)
    server.sendmail(sender, TEAM_RECIPIENTS, msg.as_string())
    server.quit()

def send_submission_notification_email(source_info, dest_info, copied_records):
    """إرسال إيميل توثيق نسخ الملفات إلى مجلد التسليم/المستودع الخارجي"""
    sender = st.secrets["sender_email"]
    password = st.secrets["app_password"]

    src_name, src_link = source_info
    dst_name, dst_link = dest_info
    total_count = len(copied_records)

    msg = MIMEMultipart("alternative")
    msg['Subject'] = f"📁 توثيق تسليم ملفات: {total_count} ملف إلى [{dst_name}]"
    msg['From'] = sender
    msg['To'] = ", ".join(TEAM_RECIPIENTS)

    rows_html = ""
    for idx, item in enumerate(copied_records, 1):
        bg = "#f8fafc" if idx % 2 == 0 else "#ffffff"
        doc_url = item.get("doc_url", "")
        action_cell = f'<a href="{doc_url}" style="color: #0284c7; text-decoration: none; font-weight: bold;">افتح الملف</a>' if doc_url else 'تم النسخ بنجاح'
        
        rows_html += f"""
        <tr style="background-color: {bg}; border-bottom: 1px solid #e2e8f0;">
          <td style="padding: 10px; text-align: center; color: #64748b; font-weight: bold;">{idx}</td>
          <td style="padding: 10px; text-align: right; color: #1e293b; font-weight: 500;">{item['clean_name']}</td>
          <td style="padding: 10px; text-align: center;">{action_cell}</td>
        </tr>
        """

    html = f"""
    <html dir="rtl">
      <body style="font-family: Arial, sans-serif; background-color: #f1f5f9; padding: 25px; margin: 0;">
        <div style="max-width: 680px; margin: auto; background-color: #ffffff; border-radius: 8px; border: 1px solid #cbd5e1; overflow: hidden;">
          <div style="background-color: #1e293b; padding: 20px; text-align: center; color: #ffffff;">
            <h2 style="margin: 0; font-size: 20px;">تقرير تسليم ونسخ المستندات</h2>
          </div>
          <div style="padding: 24px;">
            <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 16px; margin-bottom: 20px;">
              <p style="margin: 0 0 10px 0; font-size: 14px; color: #334155;">
                <b>📂 المجلد المصدر (Source):</b> 
                <a href="{src_link}" style="color: #0284c7; text-decoration: none; font-weight: bold;">{src_name}</a>
              </p>
              <p style="margin: 0; font-size: 14px; color: #334155;">
                <b>🎯 مجلد التسليم (Destination):</b> 
                <a href="{dst_link}" style="color: #16a34a; text-decoration: none; font-weight: bold;">{dst_name}</a>
              </p>
            </div>
            <p style="color: #475569; font-size: 14px; margin-bottom: 15px;"><b>إجمالي الملفات المنقولة:</b> {total_count} ملفات</p>
            <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
              <thead>
                <tr style="background-color: #f1f5f9; color: #334155; border-bottom: 2px solid #cbd5e1;">
                  <th style="padding: 10px; text-align: center; width: 10%;">#</th>
                  <th style="padding: 10px; text-align: right; width: 70%;">اسم الملف بعد التنظيف</th>
                  <th style="padding: 10px; text-align: center; width: 20%;">الرابط</th>
                </tr>
              </thead>
              <tbody>
                {rows_html}
              </tbody>
            </table>
          </div>
        </div>
      </body>
    </html>
    """
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
    server.login(sender, password)
    server.sendmail(sender, TEAM_RECIPIENTS, msg.as_string())
    server.quit()

# ==========================================
# 5. Execution Engines
# ==========================================
def execute_article_transfer(files_to_transfer, destination_folder_id, year=None, month_name=None, month_num=None, update_sheet_and_email=True):
    """نسخ ملفات الإصدار الشهري وتحديث الشيت وإرسال إيميل الإصدار"""
    gc, drive_service, docs_service = get_google_services()
    sheet_id = st.secrets["SHEET_ID"]
    
    new_records = []
    email_table_data = [["عنوان المقال", "عدد الكلمات", "رابط المستند"]]
    edition_label = f"{month_num:02d} - {month_name} {year}" if year and month_num else ""

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

        record = {
            "clean_name": final_clean_name,
            "word_count": word_count,
            "doc_url": doc_url,
            "file_id": doc_id
        }
        new_records.append(record)
        email_table_data.append([final_clean_name, word_count, doc_url])

    if update_sheet_and_email:
        all_dest_files = list_files_in_folder(drive_service, destination_folder_id)
        cached_records = {r["file_id"]: r for r in new_records}
        final_sheet_records = []
        for f in all_dest_files:
            f_id = f['id']
            if f_id in cached_records:
                final_sheet_records.append(cached_records[f_id])
            else:
                f_clean_name = strip_copy_prefix(f['name'])
                f_url = f"https://docs.google.com/document/d/{f_id}/edit"
                f_words = count_words(docs_service, f_id)
                final_sheet_records.append({
                    "clean_name": f_clean_name,
                    "word_count": f_words,
                    "doc_url": f_url,
                    "file_id": f_id
                })

        reset_and_populate_tracker(gc, sheet_id, year, month_name, month_num, final_sheet_records)
        try:
            send_monthly_notification_email(email_table_data, edition_label, sheet_id)
        except Exception as e:
            return new_records, f"تم نسخ الملفات وتحديث الجدول، لكن فشل إرسال الإيميل: {e}"

    return new_records, f"تم بنجاح نسخ {len(new_records)} مقال وتحديث جدول المتابعة والإشعار بالبريد."

def execute_submission_transfer(files_to_transfer, source_folder_id, destination_folder_id):
    """نسخ الملفات لمجلد التسليم وتوثيق ذلك بإيميل تفصيلي"""
    _, drive_service, docs_service = get_google_services()
    copied_records = []

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
        copied_records.append({
            "clean_name": final_clean_name,
            "doc_url": doc_url,
            "file_id": doc_id
        })

    # جلب أسماء وروابط المجلدين وإرسال الإيميل
    src_info = get_folder_details(drive_service, source_folder_id)
    dst_info = get_folder_details(drive_service, destination_folder_id)
    
    try:
        send_submission_notification_email(src_info, dst_info, copied_records)
    except Exception as e:
        return copied_records, f"تم نسخ {len(copied_records)} ملف إلى مجلد التسليم، لكن تعذر إرسال الإيميل: {e}"

    return copied_records, f"تم بنجاح تسليم ونسخ {len(copied_records)} ملف وتوثيق العملية بإيميل رسمي للفريق."

# ==========================================
# 6. Streamlit User Interface
# ==========================================
st.set_page_config(page_title="منظومة إدارة ونسخ المقالات", page_icon="📑", layout="wide")
st.title("منظومة إدارة ونسخ مقالات الترجمة")

tab1, tab2 = st.tabs(["📥 معالجة وإصدار المقالات الشهرية", "📂 نسخ مجلد إلى مستودع التسليم"])

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
                    with st.spinner("جاري نسخ الجديد وتحديث جدول التتبع وإرسال الإشعار..."):
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
                    with st.spinner("جاري نسخ كافة الملفات وتحديث الجدول..."):
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

# --- TAB 2: Direct Submission / Folder Cloner ---
with tab2:
    st.subheader("تسليم ونسخ محتوى مجلد كامل إلى مجلد التسليم (Submission Folder)")
    st.caption("يقوم هذا القسم بنسخ الملفات بين أي مجلدين تمتلك صلاحية عليهما، مع إزالة 'Copy of' وحفظ الأرقام، وتوثيق العملية عبر إرسال إيميل رسمي بأسماء المجلدين وقائمة الملفات.")

    if "inspection_data_tab2" not in st.session_state:
        st.session_state.inspection_data_tab2 = None

    col_src, col_dst = st.columns(2)
    with col_src:
        src_url = st.text_input("رابط المجلد المصدر (Source Folder):", key="tab2_src")
    with col_dst:
        dst_url = st.text_input("رابط مجلد التسليم (Submission/Destination Folder):", key="tab2_dst")

    if st.button("فحص ومقارنة الملفات", key="btn_check_tab2"):
        if not src_url or not dst_url:
            st.warning("يرجى إدخال رابط المجلد المصدر ورابط مجلد التسليم.")
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
        src_id = info2["src_id"]
        dst_id = info2["dst_id"]

        if not src_files:
            st.warning("المجلد المصدر لا يحتوي على أي ملفات.")
        else:
            st.write(f"📊 **إجمالي ملفات المصدر:** {len(src_files)} | **الملفات الموجودة بمجلد التسليم:** {len(info2['dst_files'])}")

            if similar_pairs:
                st.error(f"⚠️ تم رصد ({len(similar_pairs)}) ملفات موجودة أو مشابهة مسبقاً بمجلد التسليم:")
                st.dataframe(pd.DataFrame([{
                    "اسم الملف بالمصدر": p["source_name"],
                    "الاسم بعد التنظيف": p["clean_name"],
                    "الملف المقابل بالتسليم": p["dest_name"],
                    "التطابق": f"{p['similarity']}%"
                } for p in similar_pairs]), use_container_width=True)

            b1, b2, b3 = st.columns(3)
            with b1:
                if new_files and st.button(f"✅ نسخ الملفات الجديدة فقط ({len(new_files)}) وإرسال إيميل التوثيق", key="copy_new_tab2"):
                    with st.spinner("جاري النسخ وإرسال إيميل التوثيق..."):
                        _, msg = execute_submission_transfer(new_files, src_id, dst_id)
                        st.session_state.inspection_data_tab2 = None
                        st.success(msg)
                        st.balloons()

            with b2:
                if st.button(f"⚡ نسخ كافة الملفات ({len(src_files)}) وتجاوز التشابه وإرسال الإيميل", key="copy_all_tab2"):
                    with st.spinner("جاري نسخ جميع الملفات وإرسال الإيميل..."):
                        _, msg = execute_submission_transfer(src_files, src_id, dst_id)
                        st.session_state.inspection_data_tab2 = None
                        st.success(msg)
                        st.balloons()

            with b3:
                if st.button("❌ إلغاء العملية", key="cancel_tab2"):
                    st.session_state.inspection_data_tab2 = None
                    st.rerun()
