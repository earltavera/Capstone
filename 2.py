import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px

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
# 2. MOCK DATA GENERATION (Simulating LLM-extracted data)
# -----------------------------------------------------------------------------
@st.cache_data
def load_mock_data():
    np.random.seed(42)
    n_records = 200
    
    # Vocabulary lists based on the objectives
    aup_rules = [f"E14.6.1.1.{i}" for i in range(1, 10)]
    activity_types = ["Controlled", "Restricted Discretionary", "Discretionary"]
    discharge_types = ["Chemical Manufacturing", "Concrete & Asphalt Batching", "Food Processing", "Wood Processing", "Waste Management", "Foundries & Metal Coating"]
    mitigation_measures = ["Bag filters / Fabric dust collectors", "Wet scrubbers", "Biofilters", "Activated carbon adsorption", "Thermal oxidizers", "Cyclone separators"]

    data = {
        "Consent_ID": [f"BUN{10000 + i}" for i in range(n_records)],
        "Industry_Type": np.random.choice(discharge_types, n_records, p=[0.15, 0.25, 0.20, 0.15, 0.15, 0.10]),
        "AUP_E14_Rule": np.random.choice(aup_rules, n_records),
        "Activity_Type": np.random.choice(activity_types, n_records, p=[0.3, 0.5, 0.2]),
        "Mitigation_Measure": np.random.choice(mitigation_measures, n_records),
        "Consent_Duration_Years": np.random.randint(1, 31, n_records), # 1 to 30 years
        "Infringement_Count": np.random.poisson(lam=1.5, size=n_records) # Simulated historical infringements
    }
    return pd.DataFrame(data)

df = load_mock_data()

# -----------------------------------------------------------------------------
# 3. SIDEBAR FILTERS
# -----------------------------------------------------------------------------
st.sidebar.header("Filter & Search Options")
selected_industry = st.sidebar.multiselect(
    "Select Industrial Activity Type",
    options=df["Industry_Type"].unique(),
    default=df["Industry_Type"].unique()
)

selected_activity = st.sidebar.multiselect(
    "Select Activity Risk Category",
    options=df["Activity_Type"].unique(),
    default=df["Activity_Type"].unique()
)

# Apply filters
filtered_df = df[
    (df["Industry_Type"].isin(selected_industry)) & 
    (df["Activity_Type"].isin(selected_activity))
]

# -----------------------------------------------------------------------------
# 4. KPI METRICS OVERVIEW
# -----------------------------------------------------------------------------
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Total Consents Analyzed", len(filtered_df))
with col2:
    st.metric("Total Tracked Infringements", filtered_df["Infringement_Count"].sum())
with col3:
    st.metric("Avg. Consent Duration", f"{filtered_df['Consent_Duration_Years'].mean():.1f} Years")
with col4:
    st.metric("Top Mitigation System", filtered_df["Mitigation_Measure"].mode()[0])

st.markdown("---")

# -----------------------------------------------------------------------------
# 5. DASHBOARD SECTIONS (Mapping directly to Project Objectives)
# -----------------------------------------------------------------------------
tab1, tab2, tab3 = st.tabs(["📋 Rule Rankings & Risk", "🏭 Discharges & Mitigations", "⏳ Duration & Patterns"])

with tab1:
    st.header("AUP E14 Rule Rankings & Compliance Risks")
    
    col_left, col_right = st.columns(2)
    
    with col_left:
        st.subheader("1. Infringement Frequency by AUP E14 Rule")
        # Objective: Rank rules based on frequency of infringements
        rule_rankings = filtered_df.groupby("AUP_E14_Rule")["Infringement_Count"].sum().reset_index()
        rule_rankings = rule_rankings.sort_values(by="Infringement_Count", ascending=False)
        
        fig_rules = px.bar(
            rule_rankings, 
            x="Infringement_Count", 
            y="AUP_E14_Rule", 
            orientation="h",
            labels={"Infringement_Count": "Total Infringements Recorded", "AUP_E14_Rule": "AUP E14 Rule Code"},
            color="Infringement_Count",
            color_continuous_scale="Reds"
        )
        fig_rules.update_layout(yaxis={'categoryorder':'total ascending'})
        st.plotly_chart(fig_rules, use_container_width=True)

    with col_right:
        st.subheader("4. Infringed Rules Categorized by Activity Type")
        # Objective: Categorize infringed rules by activity type to assess compliance risk
        risk_df = filtered_df.groupby(["Activity_Type", "AUP_E14_Rule"])["Infringement_Count"].sum().reset_index()
        
        fig_risk = px.sunburst(
            risk_df, 
            path=["Activity_Type", "AUP_E14_Rule"], 
            values="Infringement_Count",
            color="Activity_Type",
            color_discrete_map={"Controlled": "green", "Restricted Discretionary": "orange", "Discretionary": "red"}
        )
        st.plotly_chart(fig_risk, use_container_width=True)

with tab2:
    st.header("Industrial Profile & Mitigation Profiles")
    
    col_left, col_right = st.columns(2)
    
    with col_left:
        st.subheader("2. Main Consented Industrial Air Discharges")
        # Objective: Identify and categorize the main consented industrial air discharges
        industry_counts = filtered_df["Industry_Type"].value_counts().reset_index()
        industry_counts.columns = ["Industry_Type", "Consent_Count"]
        
        fig_ind = px.pie(
            industry_counts, 
            values="Consent_Count", 
            names="Industry_Type", 
            hole=0.4,
            color_discrete_sequence=px.colors.qualitative.Pastel
        )
        st.plotly_chart(fig_ind, use_container_width=True)

    with col_right:
        st.subheader("3. Primary Engineering Mitigation Measures")
        # Objective: Identify primary mitigation measures used
        mitigation_counts = filtered_df["Mitigation_Measure"].value_counts().reset_index()
        mitigation_counts.columns = ["Mitigation_Measure", "Count"]
        
        fig_mit = px.bar(
            mitigation_counts, 
            x="Count", 
            y="Mitigation_Measure", 
            orientation="h",
            color="Count",
            color_continuous_scale="Blues"
        )
        fig_mit.update_layout(yaxis={'categoryorder':'total ascending'})
        st.plotly_chart(fig_mit, use_container_width=True)

with tab3:
    st.header("5. Regulatory Duration Patterns")
    # Objective: Analyse the distribution of consent durations (1 to 30 years)
    
    st.subheader("Distribution of Consent Durations (1 to 30 Years)")
    fig_dist = px.histogram(
        filtered_df, 
        x="Consent_Duration_Years", 
        nbins=30,
        labels={"Consent_Duration_Years": "Consent Duration (Years)"},
        color="Activity_Type",
        barmode="stack",
        color_discrete_sequence=px.colors.qualitative.Safe
    )
    fig_dist.update_layout(xaxis=dict(tickmode='linear', tick0=1, dtick=1))
    st.plotly_chart(fig_dist, use_container_width=True)

# -----------------------------------------------------------------------------
# 6. RAW DATA PREVIEW / LLM EXTRACTION VERIFICATION LOGS
# -----------------------------------------------------------------------------
st.markdown("---")
st.subheader("🔍 Extracted Data Explorer")
st.markdown("Review data items extracted by the NLP/LLM pipeline from the active council documents:")
st.dataframe(filtered_df, use_container_width=True)
