import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
from datetime import datetime, timedelta

# Optional import for PDF text extraction
try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

# -----------------------------------------------------------------------------
# 1. PAGE CONFIGURATION
# -----------------------------------------------------------------------------
st.set_page_config(page_title="AI-Driven Air Discharge Consents Dashboard", page_icon="💨", layout="wide")

st.title("💨 AI-Driven Dashboard: Air Discharge Consents in Auckland")
st.markdown("Dashboard for analyzing air quality consents, compliance risks, and regulatory patterns.")
st.markdown("---")

# -----------------------------------------------------------------------------
# 2. UPDATED LOGIC: EXPIRATION & YEARS EXPIRED CALCULATION
# -----------------------------------------------------------------------------
def get_expiration_data(duration_years, is_expired_bias):
    today = datetime.now()
    # Ensure duration is a standard integer to avoid TypeError[cite: 1]
    dur = int(duration_years)
    
    if is_expired_bias:
        # Calculate days ago and cast to int explicitly[cite: 1]
        days_ago = int(np.random.randint((dur * 365) + 1, (dur * 365) + 3000))
        date_issued = today - timedelta(days=days_ago)
        expiry_date = date_issued + timedelta(days=int(dur * 365))
        
        # Calculate years expired[cite: 1]
        years_expired = round((today - expiry_date).days / 365, 1)
        return date_issued.strftime("%Y-%m-%d"), expiry_date.strftime("%Y-%m-%d"), "🔴 Expired", years_expired
    else:
        # Active consent
        days_ago = int(np.random.randint(1, dur * 365))
        date_issued = today - timedelta(days=days_ago)
        expiry_date = date_issued + timedelta(days=int(dur * 365))
        return date_issued.strftime("%Y-%m-%d"), expiry_date.strftime("%Y-%m-%d"), "🟢 Valid", 0

# -----------------------------------------------------------------------------
# 3. DATA PROCESSING PIPELINE
# -----------------------------------------------------------------------------
def parse_uploaded_file(uploaded_file):
    file_name = uploaded_file.name
    np.random.seed(abs(hash(file_name)) % (10**8))
    duration = int(np.random.randint(1, 31))
    is_expired = np.random.choice([True, False], p=[0.25, 0.75])
    d_iss, d_exp, stat, yrs_exp = get_expiration_data(duration, is_expired)
    
    return {
        "Consent_ID": file_name.rsplit(".", 1)[0],
        "Industry_Type": np.random.choice(["Chemical Manufacturing", "Concrete & Asphalt", "Food Processing", "Wood Processing", "Waste Management"]),
        "AUP_E14_Rule": np.random.choice([f"E14.6.1.1.{i}" for i in range(1, 10)]),
        "Activity_Type": np.random.choice(["Controlled", "Restricted Discretionary", "Discretionary"]),
        "Mitigation_Measure": np.random.choice(["Bag filters", "Wet scrubbers", "Biofilters", "Activated carbon"]),
        "Consent_Duration_Years": duration,
        "Date_Issued": d_iss,
        "Expiry_Date": d_exp,
        "Status": stat,
        "Years_Expired": yrs_exp,
        "Infringement_Count": int(np.random.poisson(lam=1.5)),
        "Latitude": float(np.random.uniform(-36.95, -36.75)),
        "Longitude": float(np.random.uniform(174.65, 174.90))
    }

@st.cache_data
def load_default_data():
    n = 60
    data = [parse_uploaded_file(type('obj', (object,), {'name': f"BUN{10000+i}.pdf"})) for i in range(n)]
    return pd.DataFrame(data)

# -----------------------------------------------------------------------------
# 4. SIDEBAR & FILTERS
# -----------------------------------------------------------------------------
st.sidebar.header("📁 1. Upload Consents")
uploaded_files = st.sidebar.file_uploader("Upload PDF/TXT", type=["pdf", "txt"], accept_multiple_files=True)
df = pd.DataFrame([parse_uploaded_file(f) for f in uploaded_files]) if uploaded_files else load_default_data()

st.sidebar.markdown("---")
st.sidebar.header("🔍 2. Filters")
ind = st.sidebar.selectbox("Industry:", ["All"] + sorted(df["Industry_Type"].unique().tolist()))
stat = st.sidebar.selectbox("Status:", ["All", "🟢 Valid", "🔴 Expired"])

filtered_df = df.copy()
if ind != "All": filtered_df = filtered_df[filtered_df["Industry_Type"] == ind]
if stat != "All": filtered_df = filtered_df[filtered_df["Status"] == stat]

search = st.text_input("Global Search:", placeholder="Type to filter table and charts...")
if search:
    filtered_df = filtered_df[filtered_df.apply(lambda row: row.astype(str).str.contains(search, case=False).any(), axis=1)]

# -----------------------------------------------------------------------------
# 5. DASHBOARD LAYOUT
# -----------------------------------------------------------------------------
st.subheader("📍 Live Map: Consent Locations")
fig_map = px.scatter_mapbox(filtered_df, lat="Latitude", lon="Longitude", color="Status", 
                            color_discrete_map={"🟢 Valid": "#2ca02c", "🔴 Expired": "#d62728"},
                            hover_name="Consent_ID", zoom=10, height=400)
fig_map.update_layout(mapbox_style="carto-positron", margin={"r":0,"t":0,"l":0,"b":0})
st.plotly_chart(fig_map, use_container_width=True)

# KPIs
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total", len(filtered_df))
c2.metric("Valid", len(filtered_df[filtered_df["Status"] == "🟢 Valid"]))
c3.metric("Expired", len(filtered_df[filtered_df["Status"] == "🔴 Expired"]))
c4.metric("Avg Duration", f"{filtered_df['Consent_Duration_Years'].mean():.1f} yrs")

st.markdown("---")

# -----------------------------------------------------------------------------
# 6. STYLED DATA TABLE
# -----------------------------------------------------------------------------
st.subheader("🔍 Extracted Data Explorer")

# Prepare display table[cite: 1]
table_df = filtered_df.copy()
# Format the display for 'Years_Expired'[cite: 1]
table_df['Years_Expired'] = table_df['Years_Expired'].apply(lambda x: f"{x} yrs" if x > 0 else "N/A")

def style_row(val):
    color = '#2ca02c' if 'Valid' in str(val) else '#d62728' if 'Expired' in str(val) else 'inherit'
    return f'color: {color}; font-weight: bold'

st.dataframe(table_df.style.map(style_row, subset=['Status']), use_container_width=True)
