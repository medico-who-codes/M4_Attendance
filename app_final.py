import streamlit as st
import requests
import re
import pandas as pd
import datetime
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from fpdf import FPDF 

# --- Configuration & Theme ---
st.set_page_config(page_title="MTop Attendance Manager", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
    <style>
    .stApp { background-color: #0E1117; color: #FAFAFA; }
    .orange-text { color: #FFA500 !important; font-weight: bold; }
    .red-text { color: #FF4B4B !important; font-weight: bold; }
    .green-text { color: #00FF00 !important; font-weight: bold; }
    .period-box { 
        background-color: #1E2530; 
        padding: 12px 8px; 
        border-radius: 6px; 
        margin-bottom: 12px;
        font-size: 0.9em;
        text-align: center;
        border-top: 3px solid #4CAF50;
        box-shadow: 0 4px 6px rgba(0,0,0,0.3);
    }
    .period-box-holiday { border-top: 3px solid #FFA500; opacity: 0.7; }
    .period-box-past { border-top: 3px solid #888888; }
    .period-time { font-size: 0.85em; color: #AAAAAA; display: block; margin-bottom: 4px; }
    .sim-panel { background-color:#1E2530; padding:15px; border-radius:8px; margin-bottom:15px; }
    .stCheckbox { display: flex; justify-content: center; margin-top: 5px; }
    .btn-group { display: flex; gap: 10px; margin-bottom: 15px; justify-content: center; }
    </style>
""", unsafe_allow_html=True)

# --- TCS iON API Logic ---
def get_tcs_student_id(jsession_id):
    session = requests.Session()
    url = "https://g01.tcsion.com/cms/AttendancePeriodWiseServlet"
    params = {
        "className": "com.tcs.cmstimetable.action.attendance.ViewPeriodwiseAttendanceNewUI",
        "methodName": "checkPermissionandreturnData",
        "orgId": "827",
        "permissionId": "106434",
        "entityTypeId": "101762",
        "sId": "0"
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://g01.tcsion.com/mION/MtopGenericServlet?isSessionRequired=Y",
        "X-Requested-With": "XMLHttpRequest"
    }
    cookies = {"JSESSIONID": jsession_id}
    
    try:
        response = session.post(url, params=params, headers=headers, cookies=cookies)
        if response.status_code == 200 and response.text != "noaccess":
            data = response.json()
            return data.get("studentId")
    except Exception as e:
        pass
    return None

def fetch_attendance_data(jsession_id, student_id, session_ids):
    session = requests.Session()
    login_url = "https://g01.tcsion.com/cms/jsp/timetable/ViewPeriodwiseAttendanceNewLayout.jsp"
    attendance_url = "https://g01.tcsion.com/cms/AttendancePeriodWiseServlet"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://g01.tcsion.com/mION/",
    }
    cookies = {"JSESSIONID": jsession_id}
    
    # 1. Get CSRF Token
    response = session.get(login_url, headers=headers, cookies=cookies)
    csrf_token = session.cookies.get("CMS_CSRF")
    
    if not csrf_token:
        match = re.search(r'name="CMS_CSRF"\s+value="([\d.]+)"', response.text)
        if match: csrf_token = match.group(1)
    if not csrf_token:
        match = re.search(r'CMS_CSRF\s*=\s*"([\d.]+)"', response.text)
        if match: csrf_token = match.group(1)
        
    if not csrf_token:
        return None, None, "Failed to retrieve CSRF token. Check your JSESSIONID."

    attendance_headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Host": "g01.tcsion.com",
        "Origin": "https://g01.tcsion.com",
        "Referer": login_url,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "X-Requested-With": "XMLHttpRequest",
        "mt1": csrf_token
    }
    cookies["CMS_CSRF"] = csrf_token

    # Generate months dynamically for years 2022 to 2026
    months = [f"{m:02d}##{y}" for y in range(2022, 2027) for m in range(1, 13)]
    
    all_subject_dfs = []
    latest_date_df = None

    for sess_id in session_ids:
        # Fetch DateWise Data
        data_datewise = {
            "REFERENCE_ID": "cms_01588", "orgId": "827", "permissionId": "106434",
            "entityTypeId": "101762", "studentId": student_id, "sessionId": str(sess_id),
            "action": "subjectwise", "subjectId": "0", "activityId": "0", "sgmId": "44"
        }
        resp_date = requests.post(attendance_url, headers=attendance_headers, cookies=cookies, data=data_datewise)
        
        if resp_date.status_code == 200:
            try:
                data_json = resp_date.json()
                structured_data = {}
                for month in months:
                    month_data = data_json.get("lMonthAttendance", {}).get(month, {}).get("all", {})
                    for date, periods in month_data.items():
                        row = {"Date": date, "Present": 0, "Absent": 0}
                        for period in range(1, 10):   
                            p_str = str(period)
                            if p_str in periods:
                                parts = periods[p_str].split("##")
                                final_status = 1 if parts[9] == "1" else 0
                                row[f"Period {period}"] = final_status
                                row["Present"] += final_status
                                row["Absent"] += 1 - final_status
                            else:
                                row[f"Period {period}"] = ""
                        structured_data[date] = row
                
                if structured_data:
                    latest_date_df = pd.DataFrame(structured_data.values())
            except:
                pass

        # Fetch SubjectWise Data
        data_subjectwise = {
            "REFERENCE_ID": "cms_01588", "orgId": "827", "permissionId": "106434",
            "entityTypeId": "101762", "studentId": student_id, "sessionId": str(sess_id),
            "termId": "undefined", "action": "semesterwise", "subjectId": "0",
            "activityId": "0", "siteId": "9", "sgmId": "44"
        }
        resp_subj = requests.post(attendance_url, headers=attendance_headers, cookies=cookies, data=data_subjectwise)
        
        if resp_subj.status_code == 200:
            try:
                data_subj_json = resp_subj.json()
                attendance_details = data_subj_json.get("AttendanceDetails", {})
                pivot_data = []
                for subject_id, subject_data in attendance_details.items():
                    subject_name = None
                    theory_data, practical_data = {"Present": 0, "Absent": 0, "Percentage": 0}, {"Present": 0, "Absent": 0, "Percentage": 0}
                    for key, values in subject_data.items():
                        if values[1] == "THEORY": theory_data = {"Present": values[3], "Absent": values[4], "Percentage": values[5]}
                        elif values[1] == "PRACTICAL": practical_data = {"Present": values[3], "Absent": values[4], "Percentage": values[5]}
                        elif values[1] == "": subject_name = values[0]
                    pivot_data.append({
                        "Subject": subject_name,
                        "Theory Present": theory_data["Present"], "Theory Absent": theory_data["Absent"],
                        "Theory Percentage": theory_data["Percentage"], "Practical Present": practical_data["Present"],
                        "Practical Absent": practical_data["Absent"], "Practical Percentage": practical_data["Percentage"],
                    })
                if pivot_data:
                    all_subject_dfs.append(pd.DataFrame(pivot_data))
            except:
                pass

    if not all_subject_dfs or latest_date_df is None:
        return None, None, "No valid attendance data found for the given sessions."
        
    combined_subj_df = pd.concat(all_subject_dfs, ignore_index=True)
    return latest_date_df, combined_subj_df, "Success"

# --- Helper Functions ---
def clean_numeric_column(series):
    return pd.to_numeric(series.astype(str).str.replace('%', '', regex=False).str.strip(), errors='coerce').fillna(0)

def get_col_name(df, target):
    for col in df.columns:
        if str(col).strip().lower() == target.lower(): return col
    return None

def is_holiday(date, batch_year):
    if date.weekday() == 6: return True, "Sunday"
    common_holidays = [datetime.date(2026, 3, 31), datetime.date(2026, 4, 3), 
                       datetime.date(2026, 4, 9), datetime.date(2026, 4, 14), datetime.date(2026, 4, 23), datetime.date(2026, 5, 1)]
    if date in common_holidays: return True, "Holiday"
    
    
    if batch_year == 2022:
        if datetime.date(2026, 4, 18) <= date <= datetime.date(2026, 4, 27): return True, "Internals"
    elif batch_year == 2021:
        if datetime.date(2026, 4, 20) <= date <= datetime.date(2026, 4, 25): return True, "Internals"
        if date == datetime.date(2026, 5, 16): return True, "Send-ups"
    return False, ""

def get_bucket(batch_year, subject):
    if not subject: return subject
    s_lower = str(subject).strip().lower()
    
    if batch_year == 2021:
        if s_lower in ['general surgery', 'anaesthesiology', 'orthopedics', 'dentistry', 'operative surgery', 'surgery symposium', 'surgery']: return 'General Surgery'
        if s_lower in ['general medicine', 'infectious diseases', 'dermatology (skin)', 'radiodiagnosis', 'pulmonary medicine', 'casualty', 'psychiatry', 'medicine symposium', 'medicine']: return 'General Medicine'
        if s_lower in ['paediatrics', 'pediatrics']: return 'Pediatrics'
        if s_lower in ['obstetrics & gynaecology', 'og', 'og symposium', 'obstetrics and gynecology']: return 'Obstetrics and Gynecology'
        return subject
        
    if batch_year == 2022:
        if s_lower in ['community medicine', 'psm', 'preventive and social medicine', 'community medicine fhap']: return 'Community Medicine'
        if s_lower in ['ent', 'oto-rhino-laryngology', 'otorhinolaryngology']: return 'Otorhinolaryngology'
        if s_lower in ['ophthalmology', 'eye']: return 'Ophthalmology'
        return subject
        
    return subject

def get_period_details(date, period_num, batch_year, batch_group):
    day_name = date.strftime('%A')
    subject, p_type, is_interactive = None, None, False
    
    if batch_year == 2022:
        weekly_timetable = {
            'Monday': {1: ('Ophthalmology', 'Theory'), 2: ('Medicine', 'Theory')},
            'Tuesday': {1: ('Surgery', 'Theory'), 2: ('Community Medicine', 'Theory')},
            'Wednesday': {1: ('ENT', 'Theory'), 2: ('OG', 'Theory'), 5: ('Community Medicine FHAP', 'Practical')},
            'Thursday': {1: ('Surgery', 'Theory'), 2: ('Dermatology', 'Theory')},
            'Friday': {1: ('Ophthalmology', 'Theory'), 2: ('ENT', 'Theory')},
            'Saturday': {1: ('OG', 'Theory'), 2: ('ENT', 'Theory')}
        }
        if period_num in [1, 2, 5]:
            subject, p_type = weekly_timetable.get(day_name, {}).get(period_num, (None, None))
            
        if period_num == 3:
            p_type = 'Practical'
            if datetime.date(2026, 3, 23) <= date <= datetime.date(2026, 4, 17):
                p3_map = {'A': 'Ophthalmology', 'B': 'ENT', 'C': 'Community Medicine', 'D': 'Community Medicine'}
                subject = p3_map.get(batch_group)
            elif datetime.date(2026, 4, 27) <= date <= datetime.date(2026, 5, 10):
                p3_map = {'A': 'ENT', 'B': 'Ophthalmology', 'C': 'Dermatology', 'D': 'Medicine'}
                subject = p3_map.get(batch_group)
            elif datetime.date(2026, 5, 11) <= date <= datetime.date(2026, 5, 23):
                p3_map = {'A': 'ENT', 'B': 'Ophthalmology', 'C': 'Dermatology', 'D': 'Casualty'}
                subject = p3_map.get(batch_group)
                
        is_interactive = get_bucket(batch_year, subject) in ['Community Medicine', 'Ophthalmology', 'Otorhinolaryngology']
        
    elif batch_year == 2021:
        weekly_timetable = {
            'Monday': {1: ('Pediatrics', 'Theory'), 5: ('Orthopedics', 'Theory'), 6: ('Surgery Symposium', 'Theory')},
            'Tuesday': {1: ('Surgery', 'Theory'), 5: ('Pediatrics', 'Theory'), 6: ('Medicine Symposium', 'Theory')},
            'Wednesday': {1: ('Medicine', 'Theory'), 5: ('OG', 'Theory'), 6: ('Operative Surgery', 'Theory')},
            'Thursday': {1: ('Surgery', 'Theory'), 5: ('Pediatrics', 'Theory'), 6: ('OG Symposium', 'Theory')},
            'Friday': {1: ('OG', 'Theory'), 5: ('Pediatrics', 'Theory')},
            'Saturday': {1: ('Medicine', 'Theory')}
        }
        if period_num in [1, 5, 6]:
            subject, p_type = weekly_timetable.get(day_name, {}).get(period_num, (None, None))
            
        p2_subject = None
        if datetime.date(2026, 3, 16) <= date <= datetime.date(2026, 4, 12):
            p2_map = {'A': 'OG', 'B': 'Pediatrics', 'C': 'Medicine', 'D': 'Surgery'}
            p2_subject = p2_map.get(batch_group)
        elif datetime.date(2026, 4, 13) <= date <= datetime.date(2026, 4, 19) or datetime.date(2026, 4, 27) <= date <= datetime.date(2026, 5, 15):
            p2_map = {'A': 'Pediatrics', 'B': 'Medicine', 'C': 'Surgery', 'D': 'OG'}
            p2_subject = p2_map.get(batch_group)
            
        if period_num == 2 and p2_subject: subject, p_type = p2_subject, 'Practical'
        if period_num in [3,7] and p2_subject == 'OG': subject, p_type = 'OG', 'Practical'

        is_interactive = subject is not None 
        
    return subject, p_type, is_interactive

def generate_pdf_report(df_combined, latest_date, end_date, batch_year, batch_group, target_subjects, active_periods, sim_memory):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(200, 10, txt=f"Attendance Simulation Report - Batch {batch_year} ({batch_group})", ln=True, align='C')
    pdf.set_font("Arial", 'I', 10)
    pdf.cell(200, 10, txt=f"Generated on {datetime.datetime.now().strftime('%d %B %Y')}", ln=True, align='C')
    pdf.ln(5)
    
    pdf.set_font("Arial", 'B', 14)
    pdf.cell(200, 10, txt="Projected Cumulative Attendance", ln=True)
    pdf.set_font("Arial", size=10)
    
    for target_bucket in target_subjects:
        t_pres = t_abs = p_pres = p_abs = 0
        
        # Universal Iterative Aggregation across batches
        for _, row in df_combined.iterrows():
            subj_name = row['Subject']
            if pd.notna(subj_name) and get_bucket(batch_year, str(subj_name).strip()) == target_bucket:
                t_pres += int(clean_numeric_column(pd.Series([row['Theory Present']]))[0])
                t_abs += int(clean_numeric_column(pd.Series([row['Theory Absent']]))[0])
                p_pres += int(clean_numeric_column(pd.Series([row['Practical Present']]))[0])
                p_abs += int(clean_numeric_column(pd.Series([row['Practical Absent']]))[0])
        
        fut_t_tot = fut_p_tot = sim_t_pres = sim_t_abs = sim_p_pres = sim_p_abs = 0
        sim_dt = latest_date + datetime.timedelta(days=1)
        
        while sim_dt <= end_date:
            holiday_check, _ = is_holiday(sim_dt, batch_year)
            if not holiday_check:
                for p in active_periods:
                    sim_subj, p_type, is_int = get_period_details(sim_dt, p, batch_year, batch_group)
                    sim_bucket = get_bucket(batch_year, sim_subj) if sim_subj else None
                    if sim_bucket == target_bucket and is_int:
                        will_attend = sim_memory.get(f"{sim_dt}_{p}", True)
                        if p_type == 'Theory':
                            fut_t_tot += 1
                            if will_attend: sim_t_pres += 1
                            else: sim_t_abs += 1
                        elif p_type == 'Practical':
                            fut_p_tot += 1
                            if will_attend: sim_p_pres += 1
                            else: sim_p_abs += 1
            sim_dt += datetime.timedelta(days=1)
            
        t_fin_tot = (t_pres + t_abs) + fut_t_tot
        t_fin_p = t_pres + sim_t_pres
        t_perc = (t_fin_p / t_fin_tot * 100) if t_fin_tot > 0 else 0
        
        p_fin_tot = (p_pres + p_abs) + fut_p_tot
        p_fin_p = p_pres + sim_p_pres
        p_perc = (p_fin_p / p_fin_tot * 100) if p_fin_tot > 0 else 0
        
        if t_fin_tot > 0 or p_fin_tot > 0:
            pdf.set_font("Arial", 'B', 11)
            pdf.cell(200, 8, txt=f"- {target_bucket}:", ln=True)
            pdf.set_font("Arial", size=10)
            pdf.cell(20, 6, txt="") 
            pdf.cell(180, 6, txt=f"Theory: {t_perc:.1f}% ({t_fin_p}/{t_fin_tot}) | Practical: {p_perc:.1f}% ({p_fin_p}/{p_fin_tot})", ln=True)

    pdf.ln(5)
    pdf.set_font("Arial", 'B', 14)
    pdf.cell(200, 10, txt="Simulated Schedule Breakdown", ln=True)
    pdf.set_font("Arial", size=10)
    
    current_dt = latest_date + datetime.timedelta(days=1)
    weeks_processed = []
    
    while current_dt <= end_date:
        start_of_week = current_dt - datetime.timedelta(days=current_dt.weekday())
        if start_of_week not in weeks_processed:
            pdf.ln(3)
            pdf.set_font("Arial", 'B', 11)
            pdf.set_fill_color(220, 220, 220)
            pdf.cell(190, 8, txt=f"  Week of {start_of_week.strftime('%d %B, %Y')}", ln=True, fill=True)
            weeks_processed.append(start_of_week)
            
            # FIXED: This loop is now indented INSIDE the if block
            for i in range(6): 
                sim_day = start_of_week + datetime.timedelta(days=i)
                if latest_date < sim_day <= end_date:
                    holiday_check, h_name = is_holiday(sim_day, batch_year)
                    if holiday_check:
                        pdf.set_font("Arial", 'I', 10)
                        pdf.cell(200, 6, txt=f"    {sim_day.strftime('%A, %b %d')}: {h_name}", ln=True)
                        continue
                        
                    daily_classes = []
                    for p in active_periods:
                        subj, p_type, is_int = get_period_details(sim_day, p, batch_year, batch_group)
                        if is_int:
                            will_attend = sim_memory.get(f"{sim_day}_{p}", True)
                            status = "ATTEND" if will_attend else "SKIP" 
                            daily_classes.append(f"P{p}: {subj} ({status})")
            
                    if daily_classes:
                        pdf.set_font("Arial", 'B', 10)
                        pdf.cell(0, 6, txt=f"    {sim_day.strftime('%A, %b %d')}:", ln=True)
                        pdf.set_font("Arial", size=10)
                        pdf.set_x(25) 
                        pdf.multi_cell(0, 6, txt=" | ".join(daily_classes))
                        pdf.ln(2) 
                        
        current_dt += datetime.timedelta(days=1)

    try: return pdf.output(dest='S').encode('latin-1')
    except AttributeError: return bytes(pdf.output())

# --- Session State Management ---
if 'sim_memory' not in st.session_state: st.session_state.sim_memory = {}
if 'data_fetched' not in st.session_state: st.session_state.data_fetched = False
if 'df_date' not in st.session_state: st.session_state.df_date = None
if 'df_subj_combined' not in st.session_state: st.session_state.df_subj_combined = None

def update_sim_memory(key_name): st.session_state.sim_memory[key_name] = st.session_state[f"widget_{key_name}"]
def bulk_toggle_memory(keys, target_state):
    for key in keys: st.session_state.sim_memory[key] = target_state

# --- App Layout & Setup ---
st.title("Attendance Tracker & Simulator")

with st.expander("Data Upload & Setup", expanded=True):
    st.markdown("### Step 1: Select your details")
    col_batch, col_group = st.columns(2)
    with col_batch: batch_year = st.selectbox("Select Batch Year", [2021, 2022, 2023, 2024, 2025], index=1) # Defaulted to 2022
    with col_group: batch_group = st.radio("Select Batch Group (Batch 2021 JIPMER Karaikal - Batch D)", ['A', 'B', 'C', 'D'], horizontal=True)

    if batch_year > 2022:
        st.info("Coming Soon! Keep attending classes...")
        st.stop()

    st.markdown("---")
    st.markdown("### Step 2: Connect to TCS iON")
    st.markdown("Provide your active `JSESSIONID` from the TCS portal. We will automatically fetch all required years in the background.")
    
    jsession_id = st.text_input("Enter JSESSIONID", type="password")
    
    if st.button("Fetch & Analyze Data", type="primary"):
        if not jsession_id:
            st.error("Please enter your JSESSIONID to continue.")
            st.stop()
            
        with st.spinner("Authenticating and fetching your student ID..."):
            student_id = get_tcs_student_id(jsession_id)
            
        if not student_id:
            st.error("Failed to retrieve Student ID. Your session might be expired. Please get a fresh JSESSIONID.")
            st.stop()
            
        # Determine Session IDs based on Batch Year and Group
        if batch_year == 2022:
            session_ids = [5468, 5469, 5470]
        else: # Default 2021 routing
            if batch_group in ['A', 'B', 'C']: session_ids = [5271, 5272, 5273]
            else: session_ids = [5276, 5277, 5278]
            
        with st.spinner(f"Extracting attendance records for {len(session_ids)} years. This may take a moment..."):
            df_date, df_subj_combined, status = fetch_attendance_data(jsession_id, student_id, session_ids)
            
            if status != "Success":
                st.error(status)
                st.stop()
                
            # Save fetched data to session state to prevent refetching
            st.session_state.df_date = df_date
            st.session_state.df_subj_combined = df_subj_combined
            st.session_state.data_fetched = True
            st.success("Data successfully fetched and loaded into memory!")

# --- Only run the dashboard if data has been fetched ---
if not st.session_state.data_fetched:
    st.stop()

# Retrieve stored data
df_date = st.session_state.df_date
df_subj_combined = st.session_state.df_subj_combined

date_col = get_col_name(df_date, 'Date')
df_date[date_col] = pd.to_datetime(df_date[date_col]).dt.date
latest_date = df_date[date_col].max()

st.markdown(f"<h4 style='text-align: right; color: #4CAF50;'>Attendance dynamically updated till {latest_date.strftime('%d %B, %Y')}</h4>", unsafe_allow_html=True)

if batch_year == 2021:
    target_subjects = ['General Medicine', 'General Surgery', 'Pediatrics', 'Obstetrics and Gynecology']
    end_date = datetime.date(2026, 5, 15)
    active_periods = [1, 2, 5, 6]
else:
    target_subjects = ['Community Medicine', 'Ophthalmology', 'Otorhinolaryngology']
    end_date = datetime.date(2026, 5, 23)
    active_periods = [1, 2, 3, 5]

all_future_keys = []
temp_dt = latest_date + datetime.timedelta(days=1)
while temp_dt <= end_date:
    holiday_check, _ = is_holiday(temp_dt, batch_year)
    if not holiday_check:
        for p in active_periods:
            sim_subj, _, is_int = get_period_details(temp_dt, p, batch_year, batch_group)
            if is_int: all_future_keys.append(f"{temp_dt}_{p}")
    temp_dt += datetime.timedelta(days=1)

tab1, tab2 = st.tabs(["Calendar & Simulation", "Subject-wise Summary"])

# --- TAB 1: Calendar & Simulation ---
with tab1:
    period_times = {
        1: "8:00 AM - 9:00 AM", 2: "9:00 AM - 1:00 PM", 
        3: "11:50 AM - 12:50 PM", 4: "05:30 PM - 09:00 PM",
        5: "2:00 PM - 3:00 PM", 6: "3:00 PM - 4:30 PM",
        7: "4:30 PM - 05:30 PM", 8: "05:30 PM - 09:00 PM", 9: "09:00 PM - 11:30 PM"
    }
    
    st.markdown("### Master Simulator Controls")
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    with c1:
        if st.button("🟢 Attend All Future", use_container_width=True):
            bulk_toggle_memory(all_future_keys, True)
            st.rerun()
    with c2:
        if st.button("🔴 Leave All Future", use_container_width=True):
            bulk_toggle_memory(all_future_keys, False)
            st.rerun()
    with c3:
        pdf_data = generate_pdf_report(df_subj_combined, latest_date, end_date, batch_year, batch_group, target_subjects, active_periods, st.session_state.sim_memory)
        st.download_button(label="📄 Export Plan to PDF", data=pdf_data, file_name="Simulation_Plan.pdf", mime="application/pdf", use_container_width=True)
            
    st.markdown("---")
    cal_col, sim_col = st.columns([2.5, 1])
    
    with cal_col:
        current_dt = latest_date
        weeks = []
        
        while current_dt <= end_date:
            start_of_week = current_dt - datetime.timedelta(days=current_dt.weekday())
            if start_of_week not in weeks: weeks.append(start_of_week)
            current_dt += datetime.timedelta(days=1)
            
        week_labels = [f"Week of {w.strftime('%d %b, %Y')}" for w in weeks]
        selected_week_label = st.selectbox("Select Week to View", week_labels)
        selected_week_start = weeks[week_labels.index(selected_week_label)]
        
        week_keys = []
        for i in range(6):
            c_day = selected_week_start + datetime.timedelta(days=i)
            if c_day > latest_date and not is_holiday(c_day, batch_year)[0]:
                for p in active_periods:
                    s_subj, _, is_int = get_period_details(c_day, p, batch_year, batch_group)
                    if is_int: week_keys.append(f"{c_day}_{p}")
        
        wc1, wc2, wc3 = st.columns([1, 1, 3])
        with wc1:
            if st.button("✅ Check Week", key=f"cw_{selected_week_start}"):
                bulk_toggle_memory(week_keys, True)
                st.rerun()
        with wc2:
            if st.button("❌ Uncheck Week", key=f"uw_{selected_week_start}"):
                bulk_toggle_memory(week_keys, False)
                st.rerun()
        
        days_cols = st.columns(6)
        
        for i in range(6):
            current_day = selected_week_start + datetime.timedelta(days=i)
            with days_cols[i]:
                st.markdown(f"<h5 style='text-align:center;'>{current_day.strftime('%A')}<br><span style='font-size:0.75em; color:#aaa;'>{current_day.strftime('%d %b')}</span></h5>", unsafe_allow_html=True)
                
                holiday_check, holiday_name = is_holiday(current_day, batch_year)
                if holiday_check:
                    st.markdown(f"<div class='period-box period-box-holiday'><b>{holiday_name}</b></div>", unsafe_allow_html=True)
                    continue
                    
                for p in active_periods:
                    subject, p_type, is_interactive = get_period_details(current_day, p, batch_year, batch_group)
                    
                    if not subject:
                        
                        continue
                        
                    box_class = "period-box"
                    if current_day <= latest_date: box_class += " period-box-past"
                    
                    if current_day <= latest_date or not is_interactive:
                        status_text = ""
                        should_check_past = True
                        
                        if current_day <= latest_date and should_check_past:
                            p_col = get_col_name(df_date, f'Period {p}')
                            past_row = df_date[df_date[date_col] == current_day]
                            if not past_row.empty and p_col and pd.notna(past_row.iloc[0][p_col]) and str(past_row.iloc[0][p_col]).strip() != "":
                                status_text = "<br><span style='color:#4CAF50;'>Present</span>" if int(past_row.iloc[0][p_col]) == 1 else "<br><span style='color:#FF4B4B;'>Absent</span>"
                        
                        st.markdown(f"<div class='{box_class}'><span class='period-time'>{period_times[p]}</span><b>{subject}</b><br><span style='font-size:0.8em; color:#ccc;'>{p_type}</span>{status_text}</div>", unsafe_allow_html=True)
                    
                    else:
                        state_key = f"{current_day}_{p}"
                        current_val = st.session_state.sim_memory.get(state_key, True)
                        
                        st.markdown(f"<div class='{box_class}' style='padding-bottom: 5px;'><span class='period-time'>{period_times[p]}</span><b>{subject}</b><br><span style='font-size:0.8em; color:#ccc;'>{p_type}</span>", unsafe_allow_html=True)
                        st.checkbox("Attend", value=current_val, key=f"widget_{state_key}", on_change=update_sim_memory, args=(state_key,), label_visibility="collapsed")
                        st.markdown("</div>", unsafe_allow_html=True)

    with sim_col:
        st.markdown("### Cumulative Simulator")
        
        for target_bucket in target_subjects:
            t_pres = t_abs = p_pres = p_abs = 0
            
            # Universal Iterative Aggregation across batches
            for _, row in df_subj_combined.iterrows():
                subj_name = row['Subject']
                if pd.notna(subj_name) and get_bucket(batch_year, str(subj_name).strip()) == target_bucket:
                    t_pres += int(clean_numeric_column(pd.Series([row['Theory Present']]))[0])
                    t_abs += int(clean_numeric_column(pd.Series([row['Theory Absent']]))[0])
                    p_pres += int(clean_numeric_column(pd.Series([row['Practical Present']]))[0])
                    p_abs += int(clean_numeric_column(pd.Series([row['Practical Absent']]))[0])
            
            fut_t_tot = fut_p_tot = 0
            sim_t_pres = sim_t_abs = sim_p_pres = sim_p_abs = 0
            
            sim_dt = latest_date + datetime.timedelta(days=1)
            while sim_dt <= end_date:
                holiday_check, _ = is_holiday(sim_dt, batch_year)
                if not holiday_check:
                    for p in active_periods:
                        sim_subj, p_type, is_int = get_period_details(sim_dt, p, batch_year, batch_group)
                        sim_bucket = get_bucket(batch_year, sim_subj) if sim_subj else None
                        
                        if sim_bucket == target_bucket and is_int:
                            state_key = f"{sim_dt}_{p}"
                            will_attend = st.session_state.sim_memory.get(state_key, True)
                            
                            if p_type == 'Theory':
                                fut_t_tot += 1
                                if will_attend: sim_t_pres += 1
                                else: sim_t_abs += 1
                            elif p_type == 'Practical':
                                fut_p_tot += 1
                                if will_attend: sim_p_pres += 1
                                else: sim_p_abs += 1
                sim_dt += datetime.timedelta(days=1)
            
            def render_stat(type_name, base_p, base_a, fut_tot, sim_p, sim_a):
                base_tot = base_p + base_a
                base_perc = (base_p / base_tot * 100) if base_tot > 0 else 0
                
                if fut_tot == 0:
                    return f"<div>{type_name} Base: <b>{base_perc:.1f}%</b> <span style='font-size:0.8em; color:#aaa;'>(No future classes)</span></div>"
                else:
                    fin_tot = base_tot + fut_tot
                    fin_p = base_p + sim_p
                    fin_perc = (fin_p / fin_tot * 100) if fin_tot > 0 else 0
                    
                    color = "green-text"
                    if fin_perc < 80: color = "orange-text"
                    if fin_perc < 75: color = "red-text"
                    
                    return f"""
                    <div style='margin-bottom: 5px;'>
                        {type_name} Base: {base_perc:.1f}%<br>
                        {type_name} Projected: <span class='{color}' title='Total: {fin_tot} | Present: {fin_p}'>{fin_perc:.1f}%</span>
                    </div>
                    """

            if t_pres + t_abs + p_pres + p_abs > 0 or fut_t_tot + fut_p_tot > 0:
                st.markdown(f"<div class='sim-panel'><h4 style='margin-top:0;'>{target_bucket}</h4>", unsafe_allow_html=True)
                st.markdown(render_stat('Theory', t_pres, t_abs, fut_t_tot, sim_t_pres, sim_t_abs), unsafe_allow_html=True)
                st.markdown("<hr style='margin: 8px 0; border-color: #333;'>", unsafe_allow_html=True)
                st.markdown(render_stat('Practical', p_pres, p_abs, fut_p_tot, sim_p_pres, sim_p_abs), unsafe_allow_html=True)
                st.markdown("</div>", unsafe_allow_html=True)

# --- TAB 2: Subject Summary ---
with tab2:
    st.markdown("### Cumulative Subject-wise Attendance")
    st.info("Displays final aggregated percentages directly pulled from TCS iON across your sessions.")
    
    if batch_year in [2021, 2022]:
        for target_bucket in target_subjects:
            t_pres = t_abs = p_pres = p_abs = 0
            for _, row in df_subj_combined.iterrows():
                subj_name = row['Subject']
                if pd.notna(subj_name) and get_bucket(batch_year, str(subj_name).strip()) == target_bucket:
                    t_pres += int(clean_numeric_column(pd.Series([row['Theory Present']]))[0])
                    t_abs += int(clean_numeric_column(pd.Series([row['Theory Absent']]))[0])
                    p_pres += int(clean_numeric_column(pd.Series([row['Practical Present']]))[0])
                    p_abs += int(clean_numeric_column(pd.Series([row['Practical Absent']]))[0])
            
            t_total = t_pres + t_abs
            p_total = p_pres + p_abs
            
            if t_total > 0 or p_total > 0:
                t_perc = (t_pres / t_total * 100) if t_total > 0 else 0
                p_perc = (p_pres / p_total * 100) if p_total > 0 else 0

                t_color = '#00FF00' if t_perc >= 80 else ('#FFA500' if t_perc >= 75 else '#FF4B4B')
                p_color = '#00FF00' if p_perc >= 80 else ('#FFA500' if p_perc >= 75 else '#FF4B4B')
                
                fig = make_subplots(rows=1, cols=2, specs=[[{'type':'domain'}, {'type':'domain'}]], subplot_titles=['Theory', 'Practical'])
                fig.add_trace(go.Pie(labels=['Present', 'Absent'], values=[t_pres, t_abs], marker_colors=[t_color, '#333333'], hole=0.7, textinfo='none', hovertemplate="<b>%{label}</b>: %{value}<extra></extra>"), 1, 1)
                fig.add_trace(go.Pie(labels=['Present', 'Absent'], values=[p_pres, p_abs], marker_colors=[p_color, '#333333'], hole=0.7, textinfo='none', hovertemplate="<b>%{label}</b>: %{value}<extra></extra>"), 1, 2)
                fig.update_layout(title_text=f"<b>{target_bucket}</b> (Cumulative)", annotations=[dict(text=f"{t_perc:.1f}%", x=0.225, y=0.5, font_size=16, showarrow=False), dict(text=f"{p_perc:.1f}%", x=0.775, y=0.5, font_size=16, showarrow=False)], showlegend=False, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', height=220, margin=dict(t=50, b=10, l=10, r=10))
                st.plotly_chart(fig, use_container_width=True)
                
    else:
        for index, row in df_subj_combined.iterrows():
            subject = row['Subject']
            t_total = clean_numeric_column(pd.Series([row['Theory Present']]))[0] + clean_numeric_column(pd.Series([row['Theory Absent']]))[0]
            p_total = clean_numeric_column(pd.Series([row['Practical Present']]))[0] + clean_numeric_column(pd.Series([row['Practical Absent']]))[0]
            
            if t_total > 0 or p_total > 0:
                t_perc = (clean_numeric_column(pd.Series([row['Theory Present']]))[0] / t_total * 100) if t_total > 0 else 0
                p_perc = (clean_numeric_column(pd.Series([row['Practical Present']]))[0] / p_total * 100) if p_total > 0 else 0

                t_color = '#00FF00' if t_perc >= 80 else ('#FFA500' if t_perc >= 75 else '#FF4B4B')
                p_color = '#00FF00' if p_perc >= 80 else ('#FFA500' if p_perc >= 75 else '#FF4B4B')
                
                fig = make_subplots(rows=1, cols=2, specs=[[{'type':'domain'}, {'type':'domain'}]], subplot_titles=['Theory', 'Practical'])
                fig.add_trace(go.Pie(labels=['Present', 'Absent'], values=[clean_numeric_column(pd.Series([row['Theory Present']]))[0], clean_numeric_column(pd.Series([row['Theory Absent']]))[0]], marker_colors=[t_color, '#333333'], hole=0.7, textinfo='none', hovertemplate="<b>%{label}</b>: %{value}<extra></extra>"), 1, 1)
                fig.add_trace(go.Pie(labels=['Present', 'Absent'], values=[clean_numeric_column(pd.Series([row['Practical Present']]))[0], clean_numeric_column(pd.Series([row['Practical Absent']]))[0]], marker_colors=[p_color, '#333333'], hole=0.7, textinfo='none', hovertemplate="<b>%{label}</b>: %{value}<extra></extra>"), 1, 2)
                fig.update_layout(title_text=f"<b>{subject}</b>", annotations=[dict(text=f"{t_perc:.1f}%", x=0.225, y=0.5, font_size=16, showarrow=False), dict(text=f"{p_perc:.1f}%", x=0.775, y=0.5, font_size=16, showarrow=False)], showlegend=False, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', height=220, margin=dict(t=50, b=10, l=10, r=10))
                st.plotly_chart(fig, use_container_width=True)
