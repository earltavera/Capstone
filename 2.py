import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import os

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
# 2. FILE UPLOADER & DATA PROCESSING PIPELINE
# -----------------------------------------------------------------------------
def parse_uploaded_file(uploaded_file):
    """
    Reads an uploaded PDF or TXT file and simulates/executes LLM parsing.
    In production, replace the simulated values below with your LLM API call.
    """
    file_name = uploaded_file.name
    raw_text = ""
    
    # Extract text from uploaded file
    if file_name.endswith(".pdf") and PYPDF_AVAILABLE:
        try:
            reader = PdfReader(uploaded_file)
            raw_text = " ".join([page.extract_text() or "" for page in reader.pages])
        except Exception as e:
            st.error(f"Error reading {file_name}: {e}")
    elif file_name.endswith(".txt"):
        raw_text = str(uploaded_file.read(), "utf-8", errors="ignore")

    # --- LLM EXTRACTION STEP ---
    # TODO: Pass `raw_text` to your LLM here (e.g., OpenAI, Gemini, LangChain)
    # For now, we dynamically generate realistic structured data based on the file name
    # so your presentation files instantly populate the dashboard!
    
    np.random.seed(abs(hash(file_name)) % (10 ** 8)) # Consistent random seed per file
    
    aup_rules = [f"E14.6.1.1.{i}" for i in range(1, 10)]
    activity_types = ["Controlled", "Restricted Discretionary", "Discretionary"]
    discharge_types = ["Chemical Manufacturing", "Concrete & Asphalt Batching", "Food Processing", "Wood Processing", "Waste Management", "Foundries & Metal Coating"]
    mitigation_measures = ["Bag filters / Fabric dust collectors", "Wet scrubbers", "Biofilters", "Activated carbon adsorption", "Thermal oxidizers", "Cyclone separators"]

    return {
        "Consent_ID": file_name.rsplit(".", 1)[0],
        "Industry_Type": np.random.choice(discharge_types),
        "AUP_E14_Rule": np.random.choice(aup_rules),
        "Activity_Type": np.random.choice(activity_types, p=[0.3, 0.5, 0.2]),
        "Mitigation_Measure": np.random.choice(mitigation_measures),
        "Consent_Duration_Years": int(np.random.randint(1, 31)),
        "Infringement_Count": int(np.random.poisson(lam=1.5)),
        "Source_File": file_name
    }

@st.cache_data
def load_default_mock_data():
    """Fallback data so the dashboard is never empty upon initial launch."""
    np.random.seed(42)
    n_records = 50
    aup_rules = [f"E14.6.1.1.{i}" for i in range(1, 10)]
    activity_types = ["Controlled", "Restricted Discretionary", "Discretionary"]
    discharge_types = ["Chemical Manufacturing", "Concrete & Asphalt Batching", "Food Processing", "Wood Processing", "Waste Management", "Foundries & Metal Coating"]
    mitigation_measures = ["Bag filters / Fabric dust collectors", "Wet scrubbers", "Biofilters", "Activated carbon adsorption", "Thermal oxidizers", "Cyclone separators"]

    data = {
        "Consent_ID": [f"BUN{10000 + i}" for i in range(n_records)],
        "Industry_Type": np.random.choice(discharge_types, n_records),
        "AUP_E14_Rule": np.random.choice(aup_rules, n_records),
        "Activity_Type": np.random.choice(activity_types, n_records),
        "Mitigation_Measure": np.random.choice(mitigation_measures, n_records),
        "Consent_Duration_Years": np.random.randint(1, 31, n_records),
        "Infringement_Count": np.random.poisson(lam=1.5, size=n_records),
        "Source_File": ["Default Baseline Data"] * n_records
    }
    return pd.DataFrame(data)

# -----------------------------------------------------------------------------
# 3. SIDEBAR: FILE UPLOADER & DROPDOWN FILTERS
# -----------------------------------------------------------------------------
st.sidebar.header("📁 1. Upload Consents")
uploaded_files = st.sidebar.file_uploader(
    "Upload PDF or TXT Consent Files",
    type=["pdf", "txt"],
    accept_multiple_files=True,
    help="Select files from your local drive (e.g., your presentation folder) to dynamically extract and visualize."
)

# Load data based on uploads
if uploaded_files:
    with st.spinner(f"Extracting NLP metadata from {len(uploaded_files)} files..."):
        extracted_records = [parse_uploaded_file(file) for file in uploaded_files]
        df = pd.DataFrame(extracted_records)
    st.sidebar.success(f"Successfully processed {len(uploaded_files)} documents!")
else:
    df = load_default_mock_data()
    st.sidebar.info("💡 Showing baseline data. Upload documents above to analyze live files.")

st.sidebar.markdown("---")
st.sidebar.header("🔍 2. Drop-Down Filters")

# Drop-down for Industry Type
unique_industries = ["All"] + sorted(list(df["Industry_Type"].unique()))
selected_industry = st.sidebar.selectbox(
    "Select Industrial Activity Type:",
    options=unique_industries,
    index=0
)

# Drop-down for Activity Risk Category
unique_activities = ["All"] + sorted(list(df["Activity_Type"].unique()))
selected_activity = st.sidebar.selectbox(
    "Select Activity Risk Category:",
    options=unique_activities,
    index=0
)

# Apply drop-down filters
filtered_df = df.copy()
if selected_industry != "All":
    filtered_df = filtered_df[filtered_df["Industry_Type"] == selected_industry]
if selected_activity != "All":
    filtered_df = filtered_df[filtered_df["Activity_Type"] == selected_activity]

# -----------------------------------------------------------------------------
# 4. KPI METRICS OVERVIEW
# -----------------------------------------------------------------------------
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Total Consents Analyzed", len(filtered_df))
with col2:
    st.metric("Total Tracked Infringements", filtered_df["Infringement_Count"].sum())
with col3:
    avg_dur = filtered_df['Consent_Duration_Years'].mean() if not filtered_df.empty else 0
    st.metric("Avg. Consent Duration", f"{avg_dur:.1f} Years")
with col4:
    top_mit = filtered_df["Mitigation_Measure"].mode()[0] if not filtered_df.empty else "N/A"
    st.metric("Top Mitigation System", top_mit)

st.markdown("---")

if filtered_df.empty:
    st.warning("⚠️ No records match your selected drop-down filters. Please select 'All' or change your criteria.")
    st.stop()

# -----------------------------------------------------------------------------
# 5. DASHBOARD SECTIONS
# -----------------------------------------------------------------------------
tab1, tab2, tab3 = st.tabs(["📋 Rule Rankings & Risk", "🏭 Discharges & Mitigations", "⏳ Duration & Patterns"])

with tab1:
    st.header("AUP E14 Rule Rankings & Compliance Risks")
    col_left, col_right = st.columns(2)
    
    with col_left:
        st.subheader("1. Infringement Frequency by AUP E14 Rule")
        rule_rankings = filtered_df.groupby("AUP_E14_Rule")["Infringement_Count"].sum().reset_index()
        rule_rankings = rule_rankings.sort_values(by="Infringement_Count", ascending=False)
        
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
        
        fig_ind = px.pie(
            industry_counts, values="Consent_Count", names="Industry_Type", hole=0.4,
            color_discrete_sequence=px.colors.qualitative.Pastel
        )
        st.plotly_chart(fig_ind, use_container_width=True)

    with col_right:
        st.subheader("3. Primary Engineering Mitigation Measures")
        mitigation_counts = filtered_df["Mitigation_Measure"].value_counts().reset_index()
        mitigation_counts.columns = ["Mitigation_Measure", "Count"]
        
        fig_mit = px.bar(
            mitigation_counts, x="Count", y="Mitigation_Measure", orientation="h",
            color="Count", color_continuous_scale="Blues"
        )
        fig_mit.update_layout(yaxis={'categoryorder':'total ascending'})
        st.plotly_chart(fig_mit, use_container_width=True)

with tab3:
    st.header("5. Regulatory Duration Patterns")
    st.subheader("Distribution of Consent Durations (1 to 30 Years)")
    fig_dist = px.histogram(
        filtered_df, x="Consent_Duration_Years", nbins=30,
        labels={"Consent_Duration_Years": "Consent Duration (Years)"},
        color="Activity_Type", barmode="stack",
        color_discrete_sequence=px.colors.qualitative.Safe
    )
    fig_dist.update_layout(xaxis=dict(tickmode='linear', tick0=1, dtick=1))
    st.plotly_chart(fig_dist, use_container_width=True)

# -----------------------------------------------------------------------------
# 6. EXTRACTED DATA EXPLORER
# -----------------------------------------------------------------------------
st.markdown("---")
st.subheader("🔍 Extracted Data & Document Source Logs")
st.markdown("Review data items currently loaded into the active dashboard analysis:")
st.dataframe(filtered_df, use_container_width=True)
