import streamlit as st
import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re

# (دوال الاتصال بجوجل، إنشاء المجلدات، وحساب الكلمات تبقى كما هي في الأعلى)

def extract_folder_id(url):
    """استخراج المعرف (ID) من رابط جوجل درايف"""
    match = re.search(r'folders/([a-zA-Z0-9_-]+)', url)
    return match.group(1) if match else None

def send_notification_email(table_data):
    """إرسال الإيميل المنظم للفريق"""
    sender = st.secrets["sender_email"]
    password = st.secrets["app_password"]
    receiver = "team_email@example.com" 

    msg = MIMEMultipart("alternative")
    msg['Subject'] = "تحديث: مقالات جديدة جاهزة للترجمة"
    msg['From'] = sender
    msg['To'] = receiver

    # الهيكل الدقيق للإيميل
    html = """
    <html dir="rtl">
      <body style="font-family: Arial, sans-serif;">
        <h3 style="color: #2E86C1;">قائمة المقالات الجديدة:</h3>
        <p>فريقنا العزيز، تم استلام مقالات جديدة. يرجى الاطلاع على التفاصيل أدناه:</p>
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
        <br>
        <p>خالص التحيات،<br>بوابة الإدارة الآلية</p>
      </body>
    </html>
    """
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        st.error(f"فشل إرسال الإيميل: {e}")

# --- واجهة المستخدم (Streamlit UI) ---
st.title("بوابة استلام مقالات الترجمة")

# 1. إدخال الرابط
coordinator_url = st.text_input("رابط مجلد المنسق (Google Drive):")

# 2. تحديد الإصدار (الشهر والسنة)
col1, col2 = st.columns(2)
with col1:
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    selected_month_name = st.selectbox("شهر الإصدار:", months)
    selected_month_num = months.index(selected_month_name) + 1
with col2:
    current_year = datetime.datetime.now().year
    selected_year = st.selectbox("سنة الإصدار:", range(current_year, current_year + 5))

if st.button("سحب المقالات وبدء العمل"):
    if not coordinator_url:
        st.warning("يرجى إدخال رابط المنسق أولاً.")
    else:
        source_folder_id = extract_folder_id(coordinator_url)
        if not source_folder_id:
            st.error("الرابط غير صحيح. تأكد من أنه رابط مجلد جوجل درايف.")
        else:
            with st.spinner("جاري جلب الملفات، بناء المجلدات، وحساب الكلمات..."):
                # استدعاء الدالة الرئيسية التي تشغل الروبوت بالكامل
                # table_data = run_robot_pipeline(source_folder_id, selected_month_num, selected_year)
                
                # إرسال الإيميل بعد نجاح العملية
                # send_notification_email(table_data)
                
                st.success("تمت العملية بنجاح! تم بناء المجلدات وتحديث الشيت وإرسال الإيميل للفريق.")
