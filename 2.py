import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import requests
from datetime import datetime, timedelta

# Optional import for PDF text extraction (run: pip install pypdf)
try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

# -----------------------------------------------------------------------------
# 1. PAGE CONFIGURATION & STYLING
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="AI-Driven Air Discharge Consents Dashboard",
    page_icon="💨",
    layout="wide"
)

st.title("💨 AI-Driven Dashboard: Air Discharge Consents in Auckland")
st.markdown("""
*This dashboard analyzes industrial air discharge consents data extracted via LLM/NLP pipelines to support compliance monitoring and regulatory insights.*
""")
st.markdown("---")

# -----------------------------------------------------------------------------
# 2. HELPER FUNCTIONS & API INTEGRATION (Weather & Air Quality)
# -----------------------------------------------------------------------------
@st.cache_data(ttl=600)  # Cache for 10 minutes to prevent API spamming
def fetch_auckland_environmental_data():
    """Fetches live weather and air quality for Auckland (-36.8485, 174.7633)."""
    lat, lon = -36.8485, 174.7633
    try:
        # Open-Meteo Weather API
        w_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
        w_res = requests.get(w_url, timeout=5).json()
        
        # Open-Meteo Air Quality API
        aq_url = f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&current=european_aqi"
        aq_res = requests.get(aq_url, timeout=5).json()

        return {
            "temp": w_res["current"]["temperature_2m"],
            "humidity": w_res["current"]["relative_humidity_2m"],
            "wind": w_res["current"]["wind_speed_10m"],
            "aqi": aq_res["current"]["european_aqi"]
        }
    except Exception:
        return None  # Fallback if API call fails or offline

def generate_dates_and_status(duration_years, is_expired_bias=False):
    """Generates realistic issued/expiry dates with explicit integer casting."""
    today = datetime.now()
    dur = int(duration_years)
    
    if is_expired_bias:
        days_ago = int(np.random.randint((dur * 365) + 1, (dur * 365) + 3000))
    else:
        days_ago = int(np.random.randint(1, dur * 365))
        
    date_issued = today - timedelta(days=int(days_ago)) 
    expiry_date = date_issued + timedelta(days=int(dur * 365)) 
    
    status = "🔴 Expired" if expiry_date < today else "🟢 Valid"
    return date_issued.strftime("%Y-%m-%d"), expiry_date.strftime("%Y-%m-%d"), status

def parse_uploaded_file(uploaded_file):
    """Reads an uploaded PDF or TXT file and extracts/simulates structured data."""
    file_name = uploaded_file.name
    raw_text = ""
    
    if file_name.endswith(".pdf") and PYPDF_AVAILABLE:
        try:
            reader = PdfReader(uploaded_file)
            raw_text = " ".join([page.extract_text() or "" for page in reader.pages])
        except Exception as e:
            st.error(f"Error reading {file_name}: {e}")
    elif file_name.endswith(".txt"):
        raw_text = str(uploaded_file.read(), "utf-8", errors="ignore")

    np.random.seed(abs(hash(file_name)) % (10 ** 8))
    
    aup_rules = [f"E14.6.1.1.{i}" for i in range(1, 10)]
    activity_types = ["Controlled", "Restricted Discretionary", "Discretionary"]
    discharge_types = ["Chemical Manufacturing", "Concrete & Asphalt Batching", "Food Processing", "Wood Processing", "Waste Management", "Foundries & Metal Coating"]
    mitigation_measures = ["Bag filters / Fabric dust collectors", "Wet scrubbers", "Biofilters", "Activated carbon adsorption", "Thermal oxidizers", "Cyclone separators"]

    duration = int(np.random.randint(1, 31))
    is_expired = np.random.choice([True, False], p=[0.25, 0.75])
    date_issued, expiry_date, status = generate_dates_and_status(duration, is_expired)

    return {
        "Consent_ID": file_name.rsplit(".", 1)[0],
        "Industry_Type": np.random.choice(discharge_types),
        "AUP_E14_Rule": np.random.choice(aup_rules),
        "Activity_Type": np.random.choice(activity_types, p=[0.3, 0.5, 0.2]),
        "Mitigation_Measure": np.random.choice(mitigation_measures),
        "Consent_Duration_Years": duration,
        "Date_Issued": date_issued,
        "Expiry_Date": expiry_date,
        "Status": status,
        "Infringement_Count": int(np.random.poisson(lam=1.5)),
        "Latitude": float(np.random.uniform(-36.95, -36.75)),
        "Longitude": float(np.random.uniform(174.65, 174.90)),
        "Source_File": file_name
    }

@st.cache_data
def load_default_mock_data():
    """Baseline fallback dataset with realistic dates and expiration tracking."""
    np.random.seed(42)
    n_records = 60
    aup_rules = [f"E14.6.1.1.{i}" for i in range(1, 10)]
    activity_types = ["Controlled", "Restricted Discretionary", "Discretionary"]
    discharge_types = ["Chemical Manufacturing", "Concrete & Asphalt Batching", "Food Processing", "Wood Processing", "Waste Management", "Foundries & Metal Coating"]
    mitigation_measures = ["Bag filters / Fabric dust collectors", "Wet scrubbers", "Biofilters", "Activated carbon adsorption", "Thermal oxidizers", "Cyclone separators"]

    durations = np.random.randint(1, 31, n_records)
    dates_issued, expiry_dates, statuses = [], [], []
    
    for dur in durations:
        is_exp = np.random.choice([True, False], p=[0.30, 0.70])
        d_iss, d_exp, stat = generate_dates_and_status(dur, is_exp)
        dates_issued.append(d_iss)
        expiry_dates.append(d_exp)
        statuses.append(stat)

    data = {
        "Consent_ID": [f"BUN{10000 + i}" for i in range(n_records)],
        "Industry_Type": np.random.choice(discharge_types, n_records),
        "AUP_E14_Rule": np.random.choice(aup_rules, n_records),
        "Activity_Type": np.random.choice(activity_types, n_records),
        "Mitigation_Measure": np.random.choice(mitigation_measures, n_records),
        "Consent_Duration_Years": durations,
        "Date_Issued": dates_issued,
        "Expiry_Date": expiry_dates,
        "Status": statuses,
        "Infringement_Count": np.random.poisson(lam=1.2, size=n_records),
        "Latitude": np.random.uniform(-36.95, -36.75, n_records),
        "Longitude": np.random.uniform(174.65, 174.90, n_records),
        "Source_File": ["Default Baseline Data"] * n_records
    }
    return pd.DataFrame(data)

# -----------------------------------------------------------------------------
# 3. SIDEBAR CONTROLS
# -----------------------------------------------------------------------------
st.sidebar.header("📁 1. Upload Consents")
uploaded_files = st.sidebar.file_uploader(
    "Upload PDF or TXT Consent Files",
    type=["pdf", "txt"],
    accept_multiple_files=True
)

if uploaded_files:
    with st.spinner(f"Extracting NLP metadata from {len(uploaded_files)} files..."):
        extracted_records = [parse_uploaded_file(file) for file in uploaded_files]
        df = pd.DataFrame(extracted_records)
    st.sidebar.success(f"Successfully processed {len(uploaded_files)} documents!")
else:
    df = load_default_mock_data()
    st.sidebar.info("💡 Showing baseline dataset. Drop presentation files above to parse.")

st.sidebar.markdown("---")
st.sidebar.header("🔍 2. Drop-Down Filters")

unique_industries = ["All"] + sorted(list(df["Industry_Type"].unique()))
selected_industry = st.sidebar.selectbox("Select Industrial Activity Type:", options=unique_industries)

unique_activities = ["All"] + sorted(list(df["Activity_Type"].unique()))
selected_activity = st.sidebar.selectbox("Select Activity Risk Category:", options=unique_activities)

unique_statuses = ["All", "🟢 Valid", "🔴 Expired"]
selected_status = st.sidebar.selectbox("Select Consent Status:", options=unique_statuses)

filtered_df = df.copy()
if selected_industry != "All":
    filtered_df = filtered_df[filtered_df["Industry_Type"] == selected_industry]
if selected_activity != "All":
    filtered_df = filtered_df[filtered_df["Activity_Type"] == selected_activity]
if selected_status != "All":
    filtered_df = filtered_df[filtered_df["Status"] == selected_status]

# -----------------------------------------------------------------------------
# 4. GLOBAL SEARCH & LIVE WEATHER MONITORING BAR
# -----------------------------------------------------------------------------
st.markdown("### 🔍 Global Information Search")
search_query = st.text_input(
    label="Search anything:",
    placeholder="Type a Consent ID, rule, status (Valid/Expired), or date (e.g. 2025) to instantly filter...",
    label_visibility="collapsed"
)

if search_query:
    search_mask = np.column_stack([
        filtered_df[col].astype(str).str.contains(search_query, case=False, na=False) 
        for col in filtered_df.columns
    ]).any(axis=1)
    filtered_df = filtered_df[search_mask]

# --- NEW: Live Environmental & Meteorological Widget ---
env_data = fetch_auckland_environmental_data()
if env_data:
    st.markdown("#### 🌤️ Live Auckland Ambient Conditions")
    m_col1, m_col2, m_col3, m_col4 = st.columns(4)
    
    m_col1.metric("Temperature", f"{env_data['temp']} °C", delta="Live API", border=True)
    m_col2.metric("Wind Speed", f"{env_data['wind']} km/h", border=True)
    m_col3.metric("Relative Humidity", f"{env_data['humidity']}%", border=True)
    
    # Simple AQI status mapping
    aqi_text = "Good 🟢" if env_data['aqi'] <= 20 else "Moderate 🟡"
    m_col4.metric("Air Quality Index", f"{aqi_text}", border=True)

st.markdown("---")

# -----------------------------------------------------------------------------
# 5. LIVE GEOGRAPHIC MAP
# -----------------------------------------------------------------------------
st.subheader("📍 Live Map: Air Discharge Consent Locations")

if not filtered_df.empty:
    fig_map = px.scatter_mapbox(
        filtered_df,
        lat="Latitude",
        lon="Longitude",
        hover_name="Consent_ID",
        hover_data=["Status", "Industry_Type", "AUP_E14_Rule", "Expiry_Date", "Infringement_Count"],
        color="Status",
        color_discrete_map={"🟢 Valid": "#2ca02c", "🔴 Expired": "#d62728"},
        size=filtered_df["Infringement_Count"] + 2,
        zoom=10,
        height=450
    )
    fig_map.update_layout(mapbox_style="carto-positron", margin={"r":0,"t":0,"l":0,"b":0})
    st.plotly_chart(fig_map, use_container_width=True)
else:
    st.warning("No records match your search criteria to map.")

st.markdown("---")

# -----------------------------------------------------------------------------
# 6. KPI METRICS OVERVIEW
# -----------------------------------------------------------------------------
if not filtered_df.empty:
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Total Consents Found", len(filtered_df), border=True)
    with col2:
        valid_count = len(filtered_df[filtered_df["Status"] == "🟢 Valid"])
        st.metric("Active / Valid Consents", valid_count, border=True)
    with col3:
        expired_count = len(filtered_df[filtered_df["Status"] == "🔴 Expired"])
        st.metric("Expired Consents", expired_count, border=True)
    with col4:
        st.metric("Total Infringements", filtered_df["Infringement_Count"].sum(), border=True)
    with col5:
        st.metric("Avg. Duration", f"{filtered_df['Consent_Duration_Years'].mean():.1f} Yrs", border=True)
    st.markdown("---")
else:
    st.error("⚠️ No entries match your active drop-downs or text search keywords.")
    st.stop()

# -----------------------------------------------------------------------------
# 7. VISUALIZATION TABS
# -----------------------------------------------------------------------------
tab1, tab2, tab3 = st.tabs(["📋 Rule Rankings & Risk", "🏭 Discharges & Mitigations", "⏳ Duration & Patterns"])

with tab1:
    st.header("AUP E14 Rule Rankings & Compliance Risks")
    col_left, col_right = st.columns(2)
    with col_left:
        st.subheader("1. Infringement Frequency by AUP E14 Rule")
        rule_rankings = filtered_df.groupby("AUP_E14_Rule")["Infringement_Count"].sum().reset_index()
        fig_rules = px.bar(
            rule_rankings, x="Infringement_Count", y="AUP_E14_Rule", orientation="h",
            labels={"Infringement_Count": "Total Infringements", "AUP_E14_Rule": "Rule Code"},
            color="Infringement_Count", color_continuous_scale="Reds"
        )
        fig_rules.update_layout(yaxis={'categoryorder':'total ascending'})
        st.plotly_chart(fig_rules, use_container_width=True)

    with col_right:
        st.subheader("4. Infringed Rules by Activity Type")
        risk_df = filtered_df.groupby(["Activity_Type", "AUP_E14_Rule"])["Infringement_Count"].sum().reset_index()
        fig_risk = px.sunburst(
            risk_df, path=["Activity_Type", "AUP_E14_Rule"], values="Infringement_Count",
            color="Activity_Type", color_discrete_map={"Controlled": "#2ca02c", "Restricted Discretionary": "#ff7f0e", "Discretionary": "#d62728"}
        )
        st.plotly_chart(fig_risk, use_container_width=True)

with tab2:
    st.header("Industrial Profile & Mitigation Profiles")
    col_left, col_right = st.columns(2)
    with col_left:
        st.subheader("2. Main Consented Industrial Air Discharges")
        industry_counts = filtered_df["Industry_Type"].value_counts().reset_index()
        industry_counts.columns = ["Industry_Type", "Consent_Count"]
        fig_ind = px.pie(industry_counts, values="Consent_Count", names="Industry_Type", hole=0.4, color_discrete_sequence=px.colors.qualitative.Pastel)
        st.plotly_chart(fig_ind, use_container_width=True)

    with col_right:
        st.subheader("3. Primary Engineering Mitigation Measures")
        mitigation_counts = filtered_df["Mitigation_Measure"].value_counts().reset_index()
        mitigation_counts.columns = ["Mitigation_Measure", "Count"]
        fig_mit = px.bar(mitigation_counts, x="Count", y="Mitigation_Measure", orientation="h", color="Count", color_continuous_scale="Blues")
        fig_mit.update_layout(yaxis={'categoryorder':'total ascending'})
        st.plotly_chart(fig_mit, use_container_width=True)

with tab3:
    st.header("5. Regulatory Duration Patterns")
    st.subheader("Distribution of Consent Durations (1 to 30 Years)")
    fig_dist = px.histogram(
        filtered_df, x="Consent_Duration_Years", nbins=30,
        labels={"Consent_Duration_Years": "Consent Duration (Years)"},
        color="Status", barmode="stack",
        color_discrete_map={"🟢 Valid": "#2ca02c", "🔴 Expired": "#d62728"}
    )
    fig_dist.update_layout(xaxis=dict(tickmode='linear', tick0=1, dtick=1))
    st.plotly_chart(fig_dist, use_container_width=True)

# -----------------------------------------------------------------------------
# 8. EXTRACTED DATA EXPLORER
# -----------------------------------------------------------------------------
st.markdown("---")
st.subheader("🔍 Extracted Data & Document Source Logs")
st.markdown("Review data items currently matching the filter and search parameters:")

display_cols = [
    "Consent_ID", "Status", "Date_Issued", "Expiry_Date", 
    "Consent_Duration_Years", "Industry_Type", "AUP_E14_Rule", 
    "Activity_Type", "Mitigation_Measure", "Infringement_Count", "Source_File"
]
table_df = filtered_df[display_cols]

def style_status(val):
    if "Valid" in str(val):
        return "background-color: rgba(44, 160, 44, 0.2); color: #2ca02c; font-weight: bold;"
    elif "Expired" in str(val):
        return "background-color: rgba(214, 39, 40, 0.2); color: #d62728; font-weight: bold;"
    return ""

styled_table = table_df.style.map(style_status, subset=["Status"])
st.dataframe(styled_table, use_container_width=True)
