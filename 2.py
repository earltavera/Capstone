import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import requests
from datetime import datetime, timedelta

# Optional import for PDF text extraction
try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

# -----------------------------------------------------------------------------
# 1. PAGE CONFIGURATION & POWER BI-STYLE CUSTOM CSS
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Air Discharge Consents Tracker",
    page_icon="🇳🇿",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS matching the dark/yellow Power BI layout
st.markdown("""
<style>
    /* Dark Background across entire app */
    .stApp {
        background-color: #121212 !important;
        color: #E0E0E0 !important;
    }

    /* Top Control Banner Styling */
    .pbi-header {
        background-color: #262626;
        border: 1px solid #444;
        border-radius: 6px;
        padding: 12px 18px;
        margin-bottom: 15px;
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    
    .pbi-brand {
        background-color: #FFE800;
        color: #000;
        font-weight: 900;
        padding: 8px 14px;
        border-radius: 4px;
        font-size: 16px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }

    .pbi-time-badge {
        background-color: #FFE800;
        color: #000;
        font-weight: 800;
        padding: 6px 12px;
        border-radius: 4px;
        text-align: center;
        font-size: 13px;
    }

    /* Container Box Styling */
    div[data-testid="stVerticalBlock"] > div[style*="flex-direction: column"] > div {
        background-color: #1E1E1E;
        border: 1px solid #333333;
        border-radius: 6px;
    }

    /* Custom KPI Cards (Left Column) */
    .kpi-card-yellow {
        background-color: #FFE800;
        color: #000000;
        border-radius: 4px;
        padding: 10px 14px;
        margin-bottom: 10px;
        font-weight: bold;
    }

    .kpi-card-dark {
        background-color: #2A2A2A;
        border: 1px solid #444;
        border-left: 5px solid #FFE800;
        color: #FFFFFF;
        border-radius: 4px;
        padding: 10px 14px;
        margin-bottom: 10px;
    }

    /* Button and Widget Customization */
    .stButton>button {
        background-color: #FFE800 !important;
        color: #000000 !important;
        font-weight: bold !important;
        border: none !important;
        border-radius: 4px !important;
    }

    /* Tab bar styling matching metallic grey/yellow */
    div[data-baseweb="tab-list"] {
        gap: 8px;
        background-color: #222222;
        padding: 6px;
        border-radius: 6px;
        border: 1px solid #444;
    }

    button[data-baseweb="tab"] {
        font-size: 14px !important;
        font-weight: 700 !important;
        color: #CCCCCC !important;
        background-color: #333333 !important;
        border-radius: 4px !important;
        border: 1px solid #444 !important;
    }

    button[data-baseweb="tab"][aria-selected="true"] {
        background-color: #FFE800 !important;
        color: #000000 !important;
        border: 1px solid #FFE800 !important;
    }
</style>
""", unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# 2. HELPER FUNCTIONS & DATA ENGINES
# -----------------------------------------------------------------------------
now = datetime.now()
formatted_time = now.strftime("%d/%m/%y %I:%M %p NZST")

def generate_dates_and_status(duration_years, is_expired_bias=False):
    today = datetime.now()
    dur = int(duration_years)
    days_ago = int(np.random.randint((dur * 365) + 1, (dur * 365) + 3000)) if is_expired_bias else int(np.random.randint(1, dur * 365))
    date_issued = today - timedelta(days=int(days_ago)) 
    expiry_date = date_issued + timedelta(days=int(dur * 365)) 
    status = "🔴 Expired" if expiry_date < today else "🟢 Valid"
    return date_issued.strftime("%Y-%m-%d"), expiry_date.strftime("%Y-%m-%d"), status

@st.cache_data
def load_default_mock_data():
    np.random.seed(42)
    n_records = 60
    aup_rules = [f"E14.6.1.1.{i}" for i in range(1, 10)]
    activity_types = ["Controlled", "Restricted Discretionary", "Discretionary"]
    discharge_types = ["Chemical Mfg", "Concrete Batching", "Food Processing", "Wood Processing", "Waste Mgmt", "Foundries"]
    mitigation_measures = ["Bag filters", "Wet scrubbers", "Biofilters", "Activated carbon", "Thermal oxidizers", "Cyclones"]

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
st.sidebar.header("📁 DATA INGESTION")
if "file_uploader_key" not in st.session_state:
    st.session_state["file_uploader_key"] = 0

uploaded_files = st.sidebar.file_uploader(
    "Upload Consent Documents",
    type=["pdf", "txt"],
    accept_multiple_files=True,
    key=f"uploader_{st.session_state['file_uploader_key']}"
)

if uploaded_files:
    if st.sidebar.button("🗑️ Clear Uploads", use_container_width=True):
        st.session_state["file_uploader_key"] += 1
        st.rerun()

df = load_default_mock_data()

st.sidebar.markdown("---")
st.sidebar.header("🔍 FILTERS")
selected_industry = st.sidebar.selectbox("Industry Sector:", ["All"] + sorted(list(df["Industry_Type"].unique())))
selected_activity = st.sidebar.selectbox("Activity Risk Level:", ["All"] + sorted(list(df["Activity_Type"].unique())))
selected_status = st.sidebar.selectbox("Consent Status:", ["All", "🟢 Valid", "🔴 Expired"])

filtered_df = df.copy()
if selected_industry != "All":
    filtered_df = filtered_df[filtered_df["Industry_Type"] == selected_industry]
if selected_activity != "All":
    filtered_df = filtered_df[filtered_df["Activity_Type"] == selected_activity]
if selected_status != "All":
    filtered_df = filtered_df[filtered_df["Status"] == selected_status]

# -----------------------------------------------------------------------------
# 4. TOP EXECUTIVE HEADER (MATCHING POWER BI TOP BAR)
# -----------------------------------------------------------------------------
h_col1, h_col2, h_col3, h_col4 = st.columns([2.5, 3, 2.5, 2])

with h_col1:
    st.markdown("""
    <div class="pbi-brand">
        AUCKLAND COUNCIL AIR CONSENTS
        <div style="font-size: 11px; font-weight: normal; color: #333;">AIR DISCHARGE TRACKER</div>
    </div>
    """, unsafe_allow_html=True)

with h_col2:
    st.markdown("<span style='font-size: 12px; font-weight: bold; color: #888;'>FILTER VIEW:</span>", unsafe_allow_html=True)
    st.text_input("Search Consent ID / Rule:", placeholder="Search...", label_visibility="collapsed")

with h_col3:
    st.markdown("<span style='font-size: 12px; font-weight: bold; color: #888;'>VIEW METRIC:</span>", unsafe_allow_html=True)
    st.radio("Metric:", ["Infringements", "Consents", "Durations"], horizontal=True, label_visibility="collapsed")

with h_col4:
    st.markdown(f"""
    <div class="pbi-time-badge">
        LAST REFRESHED AT:<br>
        <span style="font-size: 14px;">{formatted_time}</span>
    </div>
    """, unsafe_allow_html=True)

st.markdown("---")

# -----------------------------------------------------------------------------
# 5. MAIN DASHBOARD BODY (2-COLUMN POWER BI GRID)
# -----------------------------------------------------------------------------
col_left, col_right = st.columns([1, 3.2])

# --- LEFT COLUMN: TRACKING CARDS ---
with col_left:
    st.markdown("<h4 style='color: #FFE800; margin-bottom: 10px;'>HOW ARE WE TRACKING?</h4>", unsafe_allow_html=True)
    
    total_consents = len(filtered_df)
    valid_consents = len(filtered_df[filtered_df["Status"] == "🟢 Valid"])
    expired_consents = len(filtered_df[filtered_df["Status"] == "🔴 Expired"])
    total_infringements = filtered_df["Infringement_Count"].sum()

    st.markdown(f"""
    <div class="kpi-card-yellow">
        TOTAL RECORDS: {total_consents}
        <div style="font-size: 11px; font-weight: normal;">Active Filter Scope</div>
    </div>
    <div class="kpi-card-dark">
        VALID CONSENTS: <span style="color: #00FF00; font-weight: bold;">{valid_consents}</span>
        <div style="font-size: 11px; color: #AAA;">Compliance Threshold Met</div>
    </div>
    <div class="kpi-card-dark">
        EXPIRED CONSENTS: <span style="color: #FF4444; font-weight: bold;">{expired_consents}</span>
        <div style="font-size: 11px; color: #AAA;">Requires Regulatory Action</div>
    </div>
    <div class="kpi-card-dark">
        INFRINGEMENTS: <span style="color: #FFE800; font-weight: bold;">{total_infringements}</span>
        <div style="font-size: 11px; color: #AAA;">AUP E14 Rule Violations</div>
    </div>
    """, unsafe_allow_html=True)

# --- RIGHT COLUMN: OVERVIEW PROGRESS BARS & SUMMARY METRICS ---
with col_right:
    st.markdown("<h4 style='color: #FFFFFF;'>OVERVIEW COMPLIANCE DATA</h4>", unsafe_allow_html=True)
    
    # Custom Progress Meter Chart (Matching image center bar)
    prog_df = pd.DataFrame({
        "Category": ["Valid Consents", "Total Infringements"],
        "Count": [valid_consents, total_infringements],
        "Target": [total_consents, total_consents * 1.5]
    })
    
    fig_progress = go.Figure()
    fig_progress.add_trace(go.Bar(
        y=prog_df["Category"], x=prog_df["Count"],
        orientation='h', name='Actual', marker_color='#FFE800'
    ))
    fig_progress.add_trace(go.Bar(
        y=prog_df["Category"], x=prog_df["Target"],
        orientation='h', name='Benchmark', marker_color='#444444'
    ))
    fig_progress.update_layout(
        barmode='stack', height=140, margin=dict(l=0, r=0, t=10, b=10),
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#FFFFFF'), showlegend=False
    )
    st.plotly_chart(fig_progress, use_container_width=True)

st.markdown("---")

# -----------------------------------------------------------------------------
# 6. LOWER DATA BREAKDOWN (BAR CHART + DATA TABLE)
# -----------------------------------------------------------------------------
t1, t2 = st.tabs(["📊 INFRINGEMENT RANKINGS & BREAKDOWN", "📍 GEOSPATIAL MAP"])

with t1:
    b_col1, b_col2 = st.columns([1.8, 2.2])
    
    with b_col1:
        st.markdown("<h5 style='color: #FFE800;'>INFRINGEMENTS BY AUP E14 RULE</h5>", unsafe_allow_html=True)
        rule_data = filtered_df.groupby("AUP_E14_Rule")["Infringement_Count"].sum().reset_index()
        fig_bar = px.bar(
            rule_data, x="Infringement_Count", y="AUP_E14_Rule", orientation="h",
            color_discrete_sequence=["#FFE800"]
        )
        fig_bar.update_layout(
            paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
            font=dict(color='#FFFFFF'), yaxis={'categoryorder':'total ascending'},
            margin=dict(l=0, r=0, t=10, b=10)
        )
        st.plotly_chart(fig_bar, use_container_width=True)

    with b_col2:
        st.markdown("<h5 style='color: #FFE800;'>DETAILED CONSENT AUDIT LOG</h5>", unsafe_allow_html=True)
        st.dataframe(
            filtered_df[["Consent_ID", "Status", "Industry_Type", "AUP_E14_Rule", "Infringement_Count"]],
            height=280,
            use_container_width=True
        )

with t2:
    st.subheader("Spatial Distribution of Consents across Auckland")
    fig_map = px.scatter_mapbox(
        filtered_df, lat="Latitude", lon="Longitude", hover_name="Consent_ID",
        color="Status", color_discrete_map={"🟢 Valid": "#00FF00", "🔴 Expired": "#FF0000"},
        zoom=9, height=350
    )
    fig_map.update_layout(mapbox_style="carto-darkmatter", margin={"r":0,"t":0,"l":0,"b":0})
    st.plotly_chart(fig_map, use_container_width=True)
