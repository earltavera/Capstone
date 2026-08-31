import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import ollama

# Optional import for PDF text extraction (run: pip install pypdf)
try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

# -----------------------------------------------------------------------------
# 1. PAGE CONFIGURATION
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="AI-Driven Air Discharge Consents Dashboard",
    page_icon="🇳🇿",
    layout="wide"
)

# -----------------------------------------------------------------------------
# 2. HELPER FUNCTIONS
# -----------------------------------------------------------------------------
@st.cache_data(ttl=600) 
def fetch_auckland_environmental_data():
    lat, lon = -36.8485, 174.7633
    try:
        w_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
        w_res = requests.get(w_url, timeout=15).json()
        
        aq_url = f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&current=european_aqi"
        aq_res = requests.get(aq_url, timeout=15).json()

        return {
            "temp": w_res["current"]["temperature_2m"],
            "humidity": w_res["current"]["relative_humidity_2m"],
            "wind": w_res["current"]["wind_speed_10m"],
            "aqi": aq_res["current"]["european_aqi"]
        }
    except Exception as e:
        st.error(f"Weather error: {e}")
        return None

def generate_dates_and_status(duration_years, is_expired_bias=False):
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
st.sidebar.title("🌱 Auckland Air Quality")
st.sidebar.button("Dashboard (Air)")
st.sidebar.button("Activity Log")
st.sidebar.button("Industry Breakdown")
st.sidebar.button("AUP Rules & Compliance")

st.sidebar.markdown("---")

if "file_uploader_key" not in st.session_state:
    st.session_state["file_uploader_key"] = 0

uploaded_files = st.sidebar.file_uploader(
    "Upload PDF or TXT Consent Files",
    type=["pdf", "txt"],
    accept_multiple_files=True,
    key=f"uploader_{st.session_state['file_uploader_key']}"
)

if uploaded_files:
    if st.sidebar.button("🗑️ Clear Uploaded Files", use_container_width=True):
        st.session_state["file_uploader_key"] += 1
        st.rerun()
    with st.spinner(f"Extracting data from {len(uploaded_files)} files..."):
        extracted_records = [parse_uploaded_file(file) for file in uploaded_files]
        df = pd.DataFrame(extracted_records)
    st.sidebar.success(f"Processed {len(uploaded_files)} documents!")
else:
    df = load_default_mock_data()
    st.sidebar.info("💡 Showing baseline data.")

st.sidebar.markdown("---")
st.sidebar.header("🔍 Filters")
unique_industries = ["All"] + sorted(list(df["Industry_Type"].unique()))
selected_industry = st.sidebar.selectbox("Industry:", options=unique_industries)

unique_statuses = ["All", "🟢 Valid", "🔴 Expired"]
selected_status = st.sidebar.selectbox("Status:", options=unique_statuses)

filtered_df = df.copy()
if selected_industry != "All":
    filtered_df = filtered_df[filtered_df["Industry_Type"] == selected_industry]
if selected_status != "All":
    filtered_df = filtered_df[filtered_df["Status"] == selected_status]

# Global Search
search_query = st.sidebar.text_input("Search anything:", placeholder="e.g. BUN10002")
if search_query:
    search_mask = np.column_stack([
        filtered_df[col].astype(str).str.contains(search_query, case=False, na=False) 
        for col in filtered_df.columns
    ]).any(axis=1)
    filtered_df = filtered_df[search_mask]

# -----------------------------------------------------------------------------
# 4. DASHBOARD HEADER
# -----------------------------------------------------------------------------
env_data = fetch_auckland_environmental_data()
if env_data:
    aqi_text = "Good 🟢" if env_data['aqi'] <= 20 else "Moderate 🟡"
    st.markdown(f"**Auckland Live:** 🌡️ {env_data['temp']}°C | 💨 {env_data['wind']} km/h | 🌫️ AQI: {aqi_text}")

# -----------------------------------------------------------------------------
# 5. TOP ROW: METRICS & MAP
# -----------------------------------------------------------------------------
col1, col2, col3, col4 = st.columns([1, 1, 1.5, 1])

if not filtered_df.empty:
    valid_count = len(filtered_df[filtered_df["Status"] == "🟢 Valid"])
    expired_count = len(filtered_df[filtered_df["Status"] == "🔴 Expired"])
    total_infringements = filtered_df["Infringement_Count"].sum()
    avg_duration = filtered_df['Consent_Duration_Years'].mean()
else:
    valid_count, expired_count, total_infringements, avg_duration = 0, 0, 0, 0

with col1:
    st.metric(label="Total Active Air Consents", value=valid_count)
    st.metric(label="Total Infringements", value=total_infringements)

with col2:
    st.metric(label="Expired Consents", value=expired_count)
    st.metric(label="Avg. Consent Duration", value=f"{avg_duration:.1f} Yrs")

with col3:
    st.markdown("**📍 Live Consent Locations**")
    if not filtered_df.empty:
        # 1. Change scatter_mapbox to scatter_map
        fig_map = px.scatter_map(
            filtered_df,
            lat="Latitude",
            lon="Longitude",
            hover_name="Consent_ID",
            hover_data=["Status", "Industry_Type", "AUP_E14_Rule"],
            color="Status",
            color_discrete_map={"🟢 Valid": "#2ca02c", "🔴 Expired": "#d62728"},
            # I added your +2 trick back so zero-infringement points don't vanish!
            size=filtered_df["Infringement_Count"] + 2, 
            zoom=9,
            height=250
        )
        # 2. Change mapbox_style to map_style
        fig_map.update_layout(map_style="carto-darkmatter", margin={"r":0,"t":0,"l":0,"b":0})
        st.plotly_chart(fig_map, use_container_width=True)
    else:
        st.warning("No records match your search criteria.")

with col4:
    st.markdown("**Consent Status**")
    if not filtered_df.empty:
        status_counts = filtered_df["Status"].value_counts().reset_index()
        fig_donut = px.pie(status_counts, values="count", names="Status", hole=0.7,
                           color="Status", color_discrete_map={"🟢 Valid": "#2ca02c", "🔴 Expired": "#d62728"})
        fig_donut.update_layout(height=250, margin=dict(t=0, b=0, l=0, r=0), showlegend=False)
        
        # Adding total count in the center
        fig_donut.add_annotation(text=f"{len(filtered_df)}<br>Total", x=0.5, y=0.5, font_size=20, showarrow=False)
        st.plotly_chart(fig_donut, use_container_width=True)

st.markdown("---")

# -----------------------------------------------------------------------------
# 6. MIDDLE ROW: CHARTS
# -----------------------------------------------------------------------------
col_chart1, col_chart2 = st.columns(2)

if not filtered_df.empty:
    with col_chart1:
        st.markdown("**Duration Pattern (Years)**")
        fig_dist = px.histogram(
            filtered_df, x="Consent_Duration_Years", nbins=30,
            color="Status", barmode="stack",
            color_discrete_map={"🟢 Valid": "#2ca02c", "🔴 Expired": "#d62728"}
        )
        fig_dist.update_layout(height=300, margin=dict(t=10, b=0, l=0, r=0))
        st.plotly_chart(fig_dist, use_container_width=True)

    with col_chart2:
        st.markdown("**Top Emitting Industries**")
        industry_counts = filtered_df["Industry_Type"].value_counts().reset_index()
        industry_counts.columns = ["Industry_Type", "Count"]
        fig_ind = px.bar(industry_counts, x="Count", y="Industry_Type", orientation="h")
        fig_ind.update_layout(yaxis={'categoryorder':'total ascending'}, height=300, margin=dict(t=10, b=0, l=0, r=0))
        st.plotly_chart(fig_ind, use_container_width=True)

st.markdown("---")

# -----------------------------------------------------------------------------
# 7. BOTTOM ROW: DATA TABLE
# -----------------------------------------------------------------------------
st.markdown("**Recent Consents & Actions**")
display_cols = [
    "Consent_ID", "Status", "Date_Issued", "Expiry_Date", 
    "Consent_Duration_Years", "Industry_Type", "AUP_E14_Rule", "Infringement_Count"
]
table_df = filtered_df[display_cols] if not filtered_df.empty else filtered_df

def style_status(val):
    if "Valid" in str(val):
        return "color: #2ca02c; font-weight: bold;"
    elif "Expired" in str(val):
        return "color: #d62728; font-weight: bold;"
    return ""

if not table_df.empty:
    styled_table = table_df.style.map(style_status, subset=["Status"])
    st.dataframe(styled_table, use_container_width=True, hide_index=True)

# -----------------------------------------------------------------------------
# 8. LOCAL DASHBOARD CHATBOT
# -----------------------------------------------------------------------------
st.markdown("---")
st.markdown("**Need setup help?**")

if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if user_prompt := st.chat_input("Ask a question about the data above..."):
    st.session_state.messages.append({"role": "user", "content": user_prompt})
    with st.chat_message("user"):
        st.markdown(user_prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                data_context = filtered_df.to_string(index=False)
                system_prompt = f"Answer the user's question using ONLY the temporary data currently visible on the dashboard below:\n\n{data_context}"
                
                response = ollama.chat(
                    model='llama3',
                    messages=[
                        {'role': 'system', 'content': system_prompt},
                        {'role': 'user', 'content': user_prompt}
                    ]
                )
                assistant_reply = response['message']['content']
                st.markdown(assistant_reply)
                st.session_state.messages.append({"role": "assistant", "content": assistant_reply})
            except Exception as e:
                st.error(f"Could not connect to local Ollama. Error: {e}")
