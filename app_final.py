from __future__ import annotations

import streamlit as st
import requests
import re
import json
import pandas as pd
import datetime
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from fpdf import FPDF 
from urllib.parse import urlparse, parse_qs

# ===========================================================================
# Portal sign-in - pure HTTP, no browser.
# ===========================================================================

MTOP_BASE = "https://mtop.tcsion.com"
DEFAULT_ORIGIN = "https://g01.tcsion.com"
MTOP_APP_ID = "9540"          # the portal shell
CMS_APP_ID = "9520"           # the CMS/attendance solution
ENTITY_TYPE_ID = "101762"
DEFAULT_SS_TAB_ID = "8984262"
QUICKLINK_ID = "4710539"   # the Periodwise Attendance quicklink
CMS_JSP_PATH = "cms/jsp/timetable/ViewPeriodwiseAttendanceNewLayout.jsp"

# Lifted verbatim from encryptText() in the login bundle.
PASSWORD_SALT = "fdledje4p2aga6gtfgq2ce"

# Accounts barred from the app, matched against the portal's own display name.
# Substring and case-insensitive, so "prathap", "PRATHAP" and "R Prathap Kumar"
# all match.
BLOCKED_NAME = re.compile(r"prathap", re.IGNORECASE)

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")


class SignInError(RuntimeError):
    """The chain did not end in a working, logged-in /cms session."""


def encode_password(raw):
    """Port of encryptText() + encode() from the login SPA.

        js:  e = this.encode(password + SALT, 4)
             return e.slice(-2) + e.slice(2, -2) + e.slice(0, 2)

    Append a fixed salt, shift every character up by 4, then rotate the last
    two characters to the front and the first two to the back.
    """
    shifted = "".join(chr(ord(c) + 4) for c in raw + PASSWORD_SALT)
    return shifted[-2:] + shifted[2:-2] + shifted[:2]


def _portal_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": BROWSER_UA,
        "Accept-Language": "en-US,en;q=0.9",
    })
    # bank every CSRF token the server hands out, on every response including
    # the ones inside redirect chains
    session.csrf = _CsrfPool()
    session.hooks["response"].append(session.csrf.harvest)
    return session


def _discover_origin(session, account, timeout, trace):
    """Step 1 - which regional server hosts this account. Sends no password."""
    resp = session.post(
        f"{MTOP_BASE}/mION/LoginServlet",
        data={"AppId": MTOP_APP_ID, "regionId": "undefined", "reqType": "null",
              "getRedirectCookie": "Y", "accountname": account},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        timeout=timeout,
    )
    trace.append(("region lookup", resp.status_code, resp.url))
    body = (resp.text or "").strip()
    parsed = urlparse(body)
    if parsed.scheme and parsed.hostname:
        return f"{parsed.scheme}://{parsed.hostname}"   # drops the :443 it appends
    return DEFAULT_ORIGIN


def _submit_credentials(session, origin, account, password, timeout, trace):
    """Step 2 - the credential post.

    These field values mirror what the page's fakeFormSubmit() puts on the
    wire. The bundle also builds a query string with isEncrypted flipped to
    "1", but that string is dead code - the form object is what gets submitted,
    so isEncrypted goes out as "0".
    """
    resp = session.post(
        f"{origin}/Login/Login",
        data={"accountname": account, "password": encode_password(password),
              "regionId": "undefined", "rememberMe": "1", "loginType": "16",
              "channel": "3", "urlType": "ngmTOPLogin", "isEncrypted": "0",
              "isPasswordEncrypted": "true"},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Origin": MTOP_BASE, "Referer": f"{MTOP_BASE}/mION/model/ng/"},
        allow_redirects=True, timeout=timeout,
    )
    trace.append(("credentials", resp.status_code, resp.url))
    return resp


def _login_rejected(resp):
    """Did the credential post bounce to the portal's rejection page?

    /Login/Login answers 200 whether or not it liked the password; the only
    signal is the redirect to /Login/loginfailure. The browser-driven version
    of this chain checked for it (tcsion_session._wait_for_login_result) and
    the HTTP rewrite dropped it, so a mistyped password ran the whole handoff
    anonymously - picking up a /cms cookie on the way, since the servlet hands
    those to anyone - and surfaced as "the attendance service refused the
    session". That points at the attendance app when the truth is much earlier
    and much simpler.
    """
    return "loginfailure" in (resp.url or "").lower()


def _clear_interstitial(session, origin, resp, timeout, trace):
    """Step 2b - get past the privacy-policy interstitial.

    A first-time login lands on /Login/PrivacyPolicyCapturePage, an empty shell
    whose scripts check whether consent was already recorded and, if so, do
    exactly one thing (inside_js/dataWebtopCapture.js):

        parent.parent.location.href = origin + "/Login/intermediatePage"

    Following that navigation is what finishes the login and sets MTOPSESSIONID,
    the cross-app cookie at Path=/ that the other webapps on the host read. Skip
    it and every webapp on the host issues its own anonymous session instead.

    An account whose consent is already on file never sees the page: the
    credential post runs through /Login/intermediatePage on its own and lands
    straight on the shell, so there is nothing here to clear. Both paths finish
    in _enter_shell, which is where the CSRF pool comes from.
    """
    if "PrivacyPolicyCapturePage" not in (resp.url or ""):
        return resp

    # the page's own first call; mirrored for fidelity, ignored if it fails
    try:
        session.post(
            f"{origin}/Login/getPrivacyPolicyDetails",
            headers={"Content-Type": "application/json;charset=UTF-8",
                     "X-Requested-With": "XMLHttpRequest", "Referer": resp.url},
            timeout=timeout,
        )
    except requests.RequestException:
        pass

    return _goto_shell(session, origin, resp.url, timeout, trace,
                       "consent handoff")


def _goto_shell(session, origin, referer, timeout, trace, label):
    """Make the navigation dataWebtopCapture.js performs: land on the shell."""
    resp = session.get(f"{origin}/Login/intermediatePage",
                       headers={"Referer": referer},
                       allow_redirects=True, timeout=timeout)
    trace.append((label, resp.status_code, resp.url))
    return resp


def _enter_shell(session, origin, resp, timeout, trace):
    """Step 2c - stand on the shell landing page holding a CSRF pool.

    The pool is only ever seeded from /mION/?launchKey=..., and the two login
    paths reach that page differently: through the privacy-policy interstitial
    on a first consent, or straight off the credential post once consent is on
    file. Seeding used to hang off the interstitial, so the second path arrived
    at _bind_cms_session with an empty pool and every guarded call came back
    "Blocking the response -- possible CSRF detected" - a sign-in that looked
    fine right up to the point the attendance app was asked for anything.
    """
    if _seed_csrf(session, resp, trace):
        return resp

    # either this is not the shell, or it rendered csrfTokens as "". Ask for
    # the handoff explicitly, and keep the retry only if it did better.
    retry = _goto_shell(session, origin, resp.url, timeout, trace, "shell handoff")
    return retry if _seed_csrf(session, retry, trace) else resp


def _launch_key(resp, session):
    """The LK the shell was handed when the login redirected into it.

    /Login/intermediatePage lands on /mION/?launchKey=...&LK=<value>, and every
    later launch reuses that same LK rather than re-deriving one. The value is
    the /Login JSESSIONID, which is also the fallback here if the landing URL
    has been lost.
    """
    lk = (parse_qs(urlparse(resp.url).query).get("LK") or [None])[0]
    if lk:
        return lk
    for cookie in session.cookies:
        if cookie.name == "JSESSIONID" and (cookie.path or "").startswith("/Login"):
            return cookie.value
    return None


class _CsrfPool:
    """The shell's CSRF tokens, which are issued by the server, not invented.

    Every response carries an `mt1` header holding one or more tokens joined by
    "@@". The client prepends them to a pool and, for each guarded request, pops
    one off the end and appends "@@" plus (15 - tokens remaining) - which is why
    the counters in a real session run -11, -10, -9 and so on.

    A made-up value earns "Blocking the response -- possible CSRF detected", so
    this is reproduced exactly rather than approximated.
    """

    def __init__(self):
        self.pool = []
        self.sources = []

    def harvest(self, response, *args, **kwargs):
        header = response.headers.get("mt1")
        if header:
            self.pool = header.split("@@") + self.pool
            self.sources.append(f"{response.status_code} {response.url[:70]}")

    def take(self):
        if not self.pool:
            return None
        token = self.pool.pop()
        return f"{token}@@{15 - len(self.pool)}"


def _display_name(resp):
    """The signed-in user's name, from the shell landing page.

    /mION/?launchKey=... injects the user object inline, in the same script
    block that carries the CSRF pool:

        var userString = [{"userDisplayName":"...","userName":"...", ...}];

    so this costs no extra request. An anonymous fetch of the same page renders
    [{"test":"test"}], which is why the lookup is guarded rather than assumed.
    """
    match = re.search(r'var\s+userString\s*=\s*(\[.*?\]);', resp.text or "", re.S)
    if not match:
        return None
    try:
        users = json.loads(match.group(1))
    except ValueError:
        return None
    if users and isinstance(users[0], dict):
        return users[0].get("userDisplayName") or users[0].get("userName")
    return None


def _seed_csrf(session, resp, trace):
    """Take the initial CSRF pool out of the shell landing page.

    /mION/?launchKey=... renders a server-injected script block:

        var csrfTokens = "tok@@tok@@tok";
        if (csrfTokens) sessionStorage.mt1 = JSON.stringify(csrfTokens.split('@@'));

    so the pool arrives inline in the HTML, not in a header - the `mt1` response
    headers only top it up afterwards. Fetching the same page without a valid
    launchKey renders csrfTokens as "", which is exactly what an anonymous
    request sees, and is why every guarded call was answered with
    "Blocking the response -- possible CSRF detected".
    """
    match = re.search(r'var\s+csrfTokens\s*=\s*"([^"]*)"', resp.text or "")
    tokens = match.group(1).split("@@") if match and match.group(1) else []
    session.csrf.pool = tokens + session.csrf.pool
    trace.append(("csrf seed", len(tokens),
                  "from the shell landing page" if tokens
                  else "EMPTY - landing page carried no tokens"))
    return len(tokens)


def _shell_post(session, origin, path, data, timeout):
    """POST the way the loaded shell does.

    With a banked token the guarded body form is used. An empty pool falls back
    to the query form, but only so the caller gets the server's own wording in
    the trace: that form is guarded too, and answers
    "Blocking the response -- possible CSRF detected". A pool that runs dry
    here means the landing page was never seeded, not that the call is safe.
    """
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        # the SPA sends the charset and posts from the Angular shell URL; both
        # are matched here because the CSRF pool would not seed without them
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Referer": f"{origin}/mION/model/ng/",
    }
    token = session.csrf.take()
    if token is None:
        return session.post(f"{origin}/mION/{path}?isSessionRequired=Y",
                            data=data, headers=headers, timeout=timeout)
    return session.post(
        f"{origin}/mION/{path}",
        data={**data, "isSessionRequired": "Y", "requestorigin": origin,
              "csrfToken": token},
        headers=headers, timeout=timeout,
    )


def _bind_cms_session(session, origin, lk, timeout, trace):
    """Step 3 - the SSO handoff that makes /cms recognise the login.

    Three calls, in the order the shell makes them:

      1. saveLoginLog     registers the session with the webtop
      2. quicklinkurl     returns the DICEDataform/ApplicationLogin.ddf base URL
                          for the attendance quicklink
      3. GetLaunchKeyForSelfService  mints a launchKey for AppID 9520

    ApplicationLogin.ddf then answers 302 and, in redirecting, binds a /cms
    session to the logged-in shell. Every parameter matters: without launchKey
    it returns 200 with an empty body and binds nothing, which is
    indistinguishable from success until the attendance servlet says
    "no access for you".
    """
    # 1. the shell records the login before launching anything
    _shell_post(session, origin, "GetDiectFormServlet",
                {"reqType": "saveLoginLog", "latitude": "", "longitude": "",
                 "screenWidth": "1440", "screenHeight": "900",
                 "deviceType": "Desktop"}, timeout)

    # 2. ask which URL the attendance quicklink points at
    resp = _shell_post(session, origin, "MtopGenericServlet",
                       {"action": "quicklinkurl", "qlid": QUICKLINK_ID}, timeout)
    base = (resp.text or "").strip().replace(":443/", "/")
    trace.append(("quicklink url", resp.status_code,
                  (base[:110] or "EMPTY") if base.startswith("http")
                  else f"unexpected: {' '.join(base.split())[:110]!r}"))
    if not base.startswith("http"):
        return None

    # 3. a launch key for the CMS app
    resp = _shell_post(session, origin, "GetLaunchKeyForSelfService",
                       {"AppId": CMS_APP_ID, "reqType": "LaunchKey"}, timeout)
    launch_key = (resp.text or "").strip()
    trace.append(("launch key", resp.status_code,
                  launch_key if launch_key.isdigit()
                  else f"unexpected: {' '.join(launch_key.split())[:110]!r}"))
    if not launch_key.isdigit():
        return None

    bind_url = f"{base}&LK={lk}&launchKey={launch_key}&AppID={CMS_APP_ID}"
    resp = session.get(bind_url, headers={"Referer": f"{origin}/mION/home.html"},
                       allow_redirects=True, timeout=timeout)
    trace.append(("cms bind", resp.status_code, resp.url[:100]))
    return resp


def cms_jsessionid(session):
    """The JSESSIONID scoped to /cms, chosen by Path rather than by eye."""
    for cookie in session.cookies:
        if cookie.name == "JSESSIONID" and (cookie.path or "").startswith("/cms"):
            return cookie.value
    return None


def sign_in(account, password, timeout=25, debug=False):
    """Log in and return (jsessionid, student_id, display_name).

    Raises SignInError if the session cannot be proved to be logged in.
    """
    session = _portal_session()
    trace = []

    origin = _discover_origin(session, account, timeout, trace)
    resp = _submit_credentials(session, origin, account, password, timeout, trace)
    if _login_rejected(resp):
        raise SignInError(
            "The portal rejected that user name or password. Check both against "
            "a direct sign-in at mtop.tcsion.com - if that works and this does "
            "not, the credentials are being altered on the way here (a trailing "
            "space is the usual culprit)."
            + _format_trace(trace)
        )

    resp = _clear_interstitial(session, origin, resp, timeout, trace)
    resp = _enter_shell(session, origin, resp, timeout, trace)
    display_name = _display_name(resp)
    if BLOCKED_NAME.search(display_name or ""):
        # Checked here rather than in the Streamlit layer: this is the first
        # point the portal has told us who signed in, and stopping now means no
        # CMS bind and no attendance fetch happen on the account's behalf.
        raise SignInError(
            "HTTP 400 - BAD REQUEST"
        )

    lk = _launch_key(resp, session)
    trace.append(("launch key", "ok" if lk else "MISSING", "LK from the landing URL"))
    if lk is None:
        raise SignInError(
            "Signed in, but the portal never handed back a launch key, so there "
            "is nothing to bind the attendance app to. Wrong credentials are the "
            "usual cause; run diagnose_login.py to see which step stopped."
            + _format_trace(trace)
        )

    _bind_cms_session(session, origin, lk, timeout, trace)
    student_id = fetch_student_id_via(session, origin, timeout)

    jsid = cms_jsessionid(session)
    if not jsid or not student_id:
        raise SignInError(
            "Signed in, but the attendance service refused the session.\n"
            f"  /cms cookie: {'present' if jsid else 'MISSING'}\n"
            f"  studentId:   {student_id or 'MISSING'}"
            + _format_trace(trace)
        )

    if debug:
        print(_format_trace(trace))
    return jsid, student_id, display_name


def _format_trace(trace):
    return "\n\nSteps:\n" + "\n".join(
        f"  [{status}] {label:<16} {detail}" for label, status, detail in trace)


def fetch_student_id_via(session, origin=DEFAULT_ORIGIN, timeout=25):
    """Ask the servlet who we are; empty means the session is not logged in."""
    try:
        resp = session.post(
            f"{origin}/cms/AttendancePeriodWiseServlet",
            params={"className": "com.tcs.cmstimetable.action.attendance"
                                 ".ViewPeriodwiseAttendanceNewUI",
                    "methodName": "checkPermissionandreturnData",
                    "orgId": "827", "permissionId": "106434",
                    "entityTypeId": ENTITY_TYPE_ID, "sId": "0"},
            headers={"Referer": f"{origin}/mION/MtopGenericServlet?isSessionRequired=Y",
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=timeout,
        )
    except requests.RequestException:
        return None
    body = (resp.text or "").strip()
    if resp.status_code != 200 or "noaccess" in body.lower().replace(" ", ""):
        return None
    try:
        return resp.json().get("studentId")
    except ValueError:
        return None

# ===========================================================================
# End of portal sign-in
# ===========================================================================

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
def login_to_tcsion(username, password):
    """Exchange credentials for a logged-in /cms session cookie."""
    return sign_in(username, password)

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
        return None, None, "Failed to retrieve CSRF token. Your session may have expired - please sign in again."

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

    # Generate months dynamically for years 2022 to 2027
    months = [f"{m:02d}##{y}" for y in range(2022, 2028) for m in range(1, 13)]
    
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
    
    if batch_year == 2022:
        # Phase IV (8th & 9th semester) Academic Calendar 2026-27 - Annexure 1
        common_holidays = {
            datetime.date(2026, 8, 15): "Independence Day", datetime.date(2026, 8, 26): "Milad-un-Nabi",
            datetime.date(2026, 9, 14): "Ganesh Chaturthi", datetime.date(2026, 10, 2): "Gandhi Jayanti",
            datetime.date(2026, 10, 20): "Vijaya Dashami", datetime.date(2026, 11, 8): "Deepavali",
            datetime.date(2026, 11, 24): "Guru Nanak's Birthday", datetime.date(2026, 12, 25): "Christmas",
            datetime.date(2027, 1, 14): "Pongal", datetime.date(2027, 1, 26): "Republic Day",
            datetime.date(2027, 3, 10): "Ramzan", datetime.date(2027, 3, 22): "Holi",
            datetime.date(2027, 3, 26): "Good Friday", datetime.date(2027, 4, 19): "Mahavir Jayanti",
            datetime.date(2027, 5, 17): "Bakri Id", datetime.date(2027, 5, 20): "Buddha Purnima"
        }
        if date in common_holidays: return True, common_holidays[date]
        if datetime.date(2026, 12, 28) <= date <= datetime.date(2027, 1, 3): return True, "Vacation"
        if datetime.date(2027, 4, 19) <= date <= datetime.date(2027, 4, 25): return True, "IA-3"
        if date >= datetime.date(2027, 5, 14): return True, "Send-ups"
        # IA-1, IA-2 and Spandan are not full holidays - clinical postings continue through them
    elif batch_year == 2023:
        common_holidays = [datetime.date(2026, 3, 31), datetime.date(2026, 4, 3), 
                           datetime.date(2026, 4, 9), datetime.date(2026, 4, 14), datetime.date(2026, 4, 23), datetime.date(2026, 5, 1)]
        if date in common_holidays: return True, "Holiday"
        if datetime.date(2026, 4, 18) <= date <= datetime.date(2026, 4, 27): return True, "Internals"
    return False, ""

def is_theory_suspended(date, batch_year):
    # Theory classes stop for IA-1, IA-2 and Spandan, but clinical postings are still scheduled
    if batch_year != 2022: return False
    if datetime.date(2026, 10, 12) <= date <= datetime.date(2026, 10, 17): return True
    if datetime.date(2026, 10, 26) <= date <= datetime.date(2026, 10, 31): return True
    if datetime.date(2027, 2, 22) <= date <= datetime.date(2027, 2, 27): return True
    return False

def get_bucket(batch_year, subject):
    if not subject: return subject
    s_lower = str(subject).strip().lower()
    
    if batch_year == 2022:
        if s_lower in ['general surgery', 'anaesthesiology', 'orthopedics', 'orthopaedics', 'dentistry', 'operative surgery', 'surgery symposium', 'surgery']: return 'General Surgery'
        if s_lower in ['general medicine', 'infectious diseases', 'dermatology (skin)', 'radiodiagnosis', 'pulmonary medicine', 'casualty', 'psychiatry', 'medicine symposium', 'medicine']: return 'General Medicine'
        if s_lower in ['paediatrics', 'pediatrics']: return 'Pediatrics'
        if s_lower in ['obstetrics & gynaecology', 'og', 'og symposium', 'obstetrics and gynecology']: return 'Obstetrics and Gynecology'
        return subject
        
    if batch_year == 2023:
        if s_lower in ['community medicine', 'psm', 'preventive and social medicine', 'community medicine fhap']: return 'Community Medicine'
        if s_lower in ['ent', 'oto-rhino-laryngology', 'otorhinolaryngology']: return 'Otorhinolaryngology'
        if s_lower in ['ophthalmology', 'eye']: return 'Ophthalmology'
        return subject
        
    return subject

def get_period_details(date, period_num, batch_year, batch_group):
    day_name = date.strftime('%A')
    subject, p_type, is_interactive = None, None, False
    
    if batch_year == 2022:
        # IX semester timetable takes over from 18 Jan 2027 (Annexure 1)
        if date >= datetime.date(2027, 1, 18):
            weekly_timetable = {
                'Monday': {1: ('Pediatrics', 'Theory'), 5: ('Orthopedics', 'Theory'), 6: ('Surgery Symposium', 'Theory')},
                'Tuesday': {1: ('Surgery', 'Theory'), 5: ('Pediatrics', 'Theory'), 6: ('Medicine Symposium', 'Theory')},
                'Wednesday': {1: ('Medicine', 'Theory'), 5: ('OG', 'Theory'), 6: ('Operative Surgery', 'Theory')},
                'Thursday': {1: ('Surgery', 'Theory'), 5: ('Pediatrics', 'Theory'), 6: ('OG Symposium', 'Theory')},
                'Friday': {1: ('OG', 'Theory'), 5: ('Pediatrics', 'Theory')},
                'Saturday': {1: ('Medicine', 'Theory')}
            }
        else:
            weekly_timetable = {
                'Monday': {1: ('Pediatrics', 'Theory'), 5: ('Orthopedics', 'Theory'), 6: ('Surgery Symposium', 'Theory')},
                'Tuesday': {1: ('Surgery', 'Theory'), 5: ('Pediatrics', 'Theory'), 6: ('Medicine Symposium', 'Theory')},
                'Wednesday': {1: ('Medicine', 'Theory'), 5: ('OG', 'Theory')},
                'Thursday': {1: ('Surgery', 'Theory'), 5: ('Pediatrics', 'Theory'), 6: ('OG Symposium', 'Theory')},
                'Friday': {1: ('OG', 'Theory'), 5: ('Orthopedics', 'Theory'), 6: ('Operative Surgery', 'Theory')},
                'Saturday': {1: ('Medicine', 'Theory')}
            }
        if period_num in [1, 5, 6] and not is_theory_suspended(date, batch_year):
            subject, p_type = weekly_timetable.get(day_name, {}).get(period_num, (None, None))
            
        p2_subject = None
        # VIII semester clinical postings
        if datetime.date(2026, 7, 27) <= date <= datetime.date(2026, 9, 6):
            p2_map = {'A': 'Medicine', 'B': 'Surgery', 'C': 'OG', 'D': 'Orthopedics'}
            p2_subject = p2_map.get(batch_group)
        elif datetime.date(2026, 9, 7) <= date <= datetime.date(2026, 10, 18):
            p2_map = {'A': 'Surgery', 'B': 'OG', 'C': 'Orthopedics', 'D': 'Medicine'}
            p2_subject = p2_map.get(batch_group)
        elif datetime.date(2026, 10, 19) <= date <= datetime.date(2026, 11, 29):
            p2_map = {'A': 'OG', 'B': 'Orthopedics', 'C': 'Medicine', 'D': 'Surgery'}
            p2_subject = p2_map.get(batch_group)
        elif datetime.date(2026, 11, 30) <= date <= datetime.date(2027, 1, 17):
            p2_map = {'A': 'Orthopedics', 'B': 'Medicine', 'C': 'Surgery', 'D': 'OG'}
            p2_subject = p2_map.get(batch_group)
        # IX semester clinical postings
        elif datetime.date(2027, 1, 18) <= date <= datetime.date(2027, 2, 14):
            p2_map = {'A': 'Medicine', 'B': 'Surgery', 'C': 'OG', 'D': 'Pediatrics'}
            p2_subject = p2_map.get(batch_group)
        elif datetime.date(2027, 2, 15) <= date <= datetime.date(2027, 3, 14):
            p2_map = {'A': 'Surgery', 'B': 'OG', 'C': 'Pediatrics', 'D': 'Medicine'}
            p2_subject = p2_map.get(batch_group)
        elif datetime.date(2027, 3, 15) <= date <= datetime.date(2027, 4, 11):
            p2_map = {'A': 'OG', 'B': 'Pediatrics', 'C': 'Medicine', 'D': 'Surgery'}
            p2_subject = p2_map.get(batch_group)
        elif datetime.date(2027, 4, 12) <= date <= datetime.date(2027, 5, 13):
            p2_map = {'A': 'Pediatrics', 'B': 'Medicine', 'C': 'Surgery', 'D': 'OG'}
            p2_subject = p2_map.get(batch_group)
            
        if period_num == 2 and p2_subject: subject, p_type = p2_subject, 'Practical'
        if period_num in [3,7] and p2_subject == 'OG': subject, p_type = 'OG', 'Practical'

        is_interactive = subject is not None 
        
    elif batch_year == 2023:
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

    # PyFPDF returns a str and wants dest='S'; fpdf2 returns a bytearray and
    # dropped the argument, raising TypeError rather than AttributeError.
    try: return pdf.output(dest='S').encode('latin-1')
    except (AttributeError, TypeError): return bytes(pdf.output())

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
    with col_batch: batch_year = st.selectbox("Select Batch Year", [2022, 2023, 2024, 2025], index=0) # Defaulted to 2022
    with col_group: batch_group = st.radio("Select Batch Group (as per the Batch list in the Academic Calendar)", ['A', 'B', 'C', 'D'], horizontal=True)

    if batch_year > 2022:
        st.info("Coming Soon! Keep attending classes...")
        st.stop()

    st.markdown("---")
    st.markdown("### Step 2: Sign in to TCS iON")
    st.markdown("Use your usual MTop credentials. The session is established in the background, so there is nothing to copy out of the browser.")
    
    col_user, col_pass = st.columns(2)
    with col_user: tcsion_username = st.text_input("Username", placeholder="P2XMBBSABC@jipmer.edu.in")
    with col_pass: tcsion_password = st.text_input("Password", type="password")
    
    if st.button("Sign In & Analyze Data", type="primary"):
        if not tcsion_username or not tcsion_password:
            st.error("Please enter both your username and password to continue.")
            st.stop()
            
        with st.spinner("Signing in to TCS iON..."):
            try:
                jsession_id, student_id, full_name = login_to_tcsion(
                    tcsion_username, tcsion_password)
            except SignInError as exc:
                st.error(str(exc))
                st.stop()
            except requests.RequestException as exc:
                st.error(f"Could not reach TCS iON: {exc}")
                st.stop()
                
        st.success(f"Signed in as {full_name or tcsion_username}")
        
        # Determine Session IDs based on Batch Year
        session_ids = [5469, 5470, 5471]
            
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

if batch_year == 2022:
    target_subjects = ['General Medicine', 'General Surgery', 'Pediatrics', 'Obstetrics and Gynecology']
    end_date = datetime.date(2027, 5, 13)
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
    if batch_year == 2023:
        period_times = {
            1: "8:00 AM - 9:00 AM", 2: "9:00 AM - 10:00 AM", 
            3: "10:00 AM - 1:00 PM", 4: "05:30 PM - 09:00 PM",
            5: "2:00 PM - 4:30 PM", 6: "3:00 PM - 4:30 PM",
            7: "4:30 PM - 05:30 PM", 8: "05:30 PM - 09:00 PM", 9: "09:00 PM - 11:30 PM"
        }
    else:
        # Default 2022 timings
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
    
    if batch_year in [2022, 2023]:
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
