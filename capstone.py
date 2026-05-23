import streamlit as st
import pandas as pd
import pymupdf
fitz = pymupdf
import re
from datetime import datetime, timedelta
import plotly.express as px
from sentence_transformers import SentenceTransformer, util
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import base64
import os
from dotenv import load_dotenv
import csv
import io
import requests
import pytz
import json
import time

# --- LLM Specific Imports ---
import google.generativeai as genai
from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage
# --- End LLM Specific Imports ---

# --- API Key Setup ---
load_dotenv()
groq_api_key = os.getenv("GROQ_API_KEY") or st.secrets.get("GROQ_API_KEY")
google_api_key = os.getenv("GOOGLE_API_KEY") or st.secrets.get("GOOGLE_API_KEY")
openweathermap_api_key = os.getenv("OPENWEATHER_API_KEY") or st.secrets.get("OPENWEATHER_API_KEY")

# ------------------------
# Streamlit Page Config & Style
# ------------------------
st.set_page_config(page_title="Auckland Air Discharge Consent Dashboard", layout="wide", page_icon="🇳🇿", initial_sidebar_state="expanded")

if google_api_key:
    genai.configure(api_key=google_api_key)
else:
    st.error("Google API key not found. Gemini AI will be offline and extraction will fail.")

# --- Weather Function ---
@st.cache_data(ttl=600)
def get_auckland_weather():
    if not openweathermap_api_key:
        return "Sunny, 18°C (offline mode)"
    url = f"https://api.openweathermap.org/data/2.5/weather?q=Auckland,nz&units=metric&appid={openweathermap_api_key}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        if data.get("cod") != 200:
            return "Weather unavailable"
        temp = data["main"]["temp"]
        desc = data["weather"][0]["description"].title()
        return f"{desc}, {temp:.1f}°C"
    except Exception:
        return "Weather unavailable"

# --- Date, Time & Weather Banner ---
nz_time = datetime.now(pytz.timezone("Pacific/Auckland"))
today = nz_time.strftime("%A, %d %B %Y")
current_time = nz_time.strftime("%I:%M %p")
weather = get_auckland_weather()

st.markdown(f"""
    <div style='text-align:center; padding:12px; font-size:1.2em; background-color:#656e6b;
                 border-radius:10px; margin-bottom:15px; font-weight:500; color:white;'>
        📍 <strong>Auckland</strong> &nbsp;&nbsp;&nbsp; 📅 <strong>{today}</strong> &nbsp;&nbsp;&nbsp; ⏰ <strong>{current_time}</strong> &nbsp;&nbsp;&nbsp; 🌦️ <strong>{weather}</strong>
    </div>
""", unsafe_allow_html=True)

st.markdown("""
    <div style="text-align: center;">
        <h2 style='color:#004489; font-family: Quicksand, sans-serif; font-size: 2.7em;'>
            Auckland Air Discharge Consent Dashboard
        </h2>
        <p style='font-size: 1.1em; color: #dc002e;'>
            Upload Air Discharge Resource Consent Decision Reports (PDFs) to automatically extract, categorize, and analyze regulatory data using Large Language Models.
        </p>
    </div>
    <br>
""", unsafe_allow_html=True)

# --- Utility Functions ---
@st.cache_data(show_spinner=False)
def geocode_address(address):
    if not address or address == "Not specified" or address == "Quota/API Error":
        return (None, None)
    standardized_address = address.strip()
    if not re.search(r'auckland', standardized_address, re.IGNORECASE):
        standardized_address += ", Auckland"
    
    geolocator = Nominatim(user_agent="air_discharge_dashboard")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.5)

    try:
        location = geocode(standardized_address)
        if location:
            return (location.latitude, location.longitude)
    except Exception:
        pass
    return (None, None)

def check_expiry(expiry_date):
    if pd.isna(expiry_date):
        return "Unknown"
    current_nz_time = datetime.now(pytz.timezone("Pacific/Auckland")).replace(tzinfo=None)
    return "Expired" if expiry_date < current_nz_time else "Active"

# --- ROBUST LLM EXTRACTION PIPELINE WITH EXPONENTIAL BACKOFF RETRIES ---
@st.cache_data(show_spinner=False)
def llm_extract_structured_data(text, file_name):
    """
    Passes raw PDF text to Gemini to extract specific data points 
    required by Auckland Council into a structured JSON format with robust retry limits.
    """
    system_instruction = """
    You are an expert environmental regulatory assistant for Auckland Council. 
    Analyze the provided Air Discharge Consent document and extract the information into a strict JSON format.
    
    Return ONLY a valid JSON object matching this schema exactly. Do not include markdown blocks.
    {
      "Resource Consent Numbers": "string",
      "Company Name": "string",
      "Address": "string (just the physical site address)",
      "Issue Date": "DD/MM/YYYY",
      "Expiry Date": "DD/MM/YYYY",
      "AUP_E14_Rules_Infringed": [
        {
          "rule": "string (e.g., E14.4.1(A14))",
          "activity_status": "string (Controlled, Restricted Discretionary, Discretionary, or Non-Complying)"
        }
      ],
      "Industrial_Activity_Category": "string (Categorize the main activity into a short phrase, e.g., Abrasive Blasting, Chemical Manufacturing, Food Processing, Crematoria, Spray Painting, Mineral Processing)",
      "Mitigation_Measures": ["string", "string"]
    }
    
    Rules for Extraction:
    - If a string cannot be found, output "Not specified".
    - If a list cannot be found, output [].
    - Under Mitigation_Measures, list physical equipment or actions mentioned in conditions (e.g., Baghouse filter, 15m Stack height, Wet scrubber, Water carts).
    - Deduce the activity status for the E14 rules directly from the document text.
    """
    
    # Prune incoming text string to roughly 15,000 chars to respect free tier token limits 
    trimmed_text = text[:10000] 
    
    max_retries = 5
    initial_delay = 5  # Start structural recovery at a baseline 5s delay
    
    for attempt in range(max_retries):
        try:
            model = genai.GenerativeModel(
                "gemini-2.0-flash",
                generation_config={"response_mime_type": "application/json"}
            )
            prompt = f"{system_instruction}\n\nDOCUMENT TEXT:\n{trimmed_text}"
            
            response = model.generate_content(prompt)
            extracted_data = json.loads(response.text)
            
            # Map structural text blob back for vector mapping/search consistency
            extracted_data["Text Blob"] = text 
            return extracted_data
            
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "Quota exceeded" in error_msg:
                if attempt < max_retries - 1:
                    sleep_time = initial_delay * (2 ** attempt)
                    st.warning(f"Rate limit hit for {file_name}. Backing off. Retrying in {sleep_time}s (Attempt {attempt + 1}/{max_retries})...")
                    time.sleep(sleep_time)
                    continue
            
            # Drop structure to standard exception default mappings if retry bounds fail
            return {
                "Resource Consent Numbers": "Quota/API Error",
                "Company Name": "Quota/API Error",
                "Address": "Not specified",
                "Issue Date": "Not specified",
                "Expiry Date": "Not specified",
                "AUP_E14_Rules_Infringed": [],
                "Industrial_Activity_Category": "Error",
                "Mitigation_Measures": [],
                "Text Blob": text
            }

def log_ai_chat(question, answer):
    timestamp = datetime.now(pytz.timezone("Pacific/Auckland")).strftime("%Y-%m-%d %H:%M:%S")
    log_entry = {"Timestamp": timestamp, "Question": question, "Answer": answer}
    file_exists = os.path.isfile("ai_chat_log.csv")
    try:
        with open("ai_chat_log.csv", mode="a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["Timestamp", "Question", "Answer"])
            if not file_exists:
                writer.writeheader()
            writer.writerow(log_entry)
    except Exception:
        pass

# --- Sidebar & Model Loader ---
st.sidebar.markdown("<h2 style='color:#1E90FF;'>Control Panel</h2>", unsafe_allow_html=True)
model_name = st.sidebar.selectbox("Choose Embedding Model (Semantic Search):", [
    "all-MiniLM-L6-v2", "multi-qa-MiniLM-L6-cos-v1"
])
uploaded_files = st.sidebar.file_uploader("Upload PDF files", type=["pdf"], accept_multiple_files=True)
query_input = st.sidebar.text_input("LLM Semantic Search Query")

@st.cache_resource
def load_embedding_model(name):
    return SentenceTransformer(name)

embedding_model = load_embedding_model(model_name)

df = pd.DataFrame()

# --- File Processing & Dashboard Loop ---
if uploaded_files:
    my_bar = st.progress(0, text="Initializing Pipeline...")
    all_data = []
    total_files = len(uploaded_files)

    for i, file in enumerate(uploaded_files):
        my_bar.progress(int(((i + 1) / total_files) * 60), text=f"Step 1/3: Parsing document {i+1}/{total_files}...")
        try:
            file_bytes = file.read()
            with fitz.open(stream=file_bytes, filetype="pdf") as doc:
                text = "\n".join(page.get_text() for page in doc)
            
            # Map structural details straight through our LLM extraction function
            data = llm_extract_structured_data(text, file.name)
            data["__file_name__"] = file.name
            data["__file_bytes__"] = file_bytes
            all_data.append(data)
            
            # Introduce an inline cooling delay to protect API parameters
            time.sleep(3) 
        except Exception as e:
            st.error(f"Error processing {file.name}: {e}")

    if all_data:
        my_bar.progress(75, text="Step 2/3: Structural Geocoding Coordinates...")
        df = pd.DataFrame(all_data)
        
        # Pull geolocational features down
        df["Latitude"], df["Longitude"] = zip(*df["Address"].apply(geocode_address))

        my_bar.progress(90, text="Step 3/3: Evaluating Spatial Rule Constraints...")

        # Parsing localized formats
        df['Issue Date'] = pd.to_datetime(df['Issue Date'], format='%d/%m/%Y', errors='coerce')
        df['Expiry Date'] = pd.to_datetime(df['Expiry Date'], format='%d/%m/%Y', errors='coerce')
        
        # Map statuses
        df["Consent Status"] = df['Expiry Date'].apply(check_expiry)
        
        # Pandas Delta Analysis: Consent Duration Mapping (Requirement 5)
        df['Duration_Years'] = (df['Expiry Date'] - df['Issue Date']).dt.days / 365.25
        duration_bins = [0, 1.5, 3.5, 7.5, 12.5, 17.5, 25, 35]
        duration_labels = ['1 year', '2 years', '5 years', '10 years', '15 years', '20 years', '30 years']
        df['Consent Duration (Bins)'] = pd.cut(df['Duration_Years'], bins=duration_bins, labels=duration_labels)

        # --- RENDERING ENGINE ---

        # Summary KPIs
        st.subheader("Consent Summary Metrics")
        col1, col2, col3 = st.columns(3)
        total_consents = len(df)
        active_count = (df["Consent Status"] == "Active").sum()
        expired_count = (df["Consent Status"] == "Expired").sum()
        
        col1.metric("Total Consents Loaded", total_consents)
        col2.metric("Active Consents", active_count)
        col3.metric("Expired Consents", expired_count)
        st.markdown("---")

        # COUNCIL CAPSTONE GRAPHICAL INSIGHTS (Auckland Council Direct Target Mappings)
        st.header("Auckland Council Capstone Analytics")
        
        chart_row1_left, chart_row1_right = st.columns(2)

        # Requirement 2: Categorization of Dominant Industrial Activities
        with chart_row1_left:
            st.subheader("Dominant Industrial Activities")
            activity_counts = df["Industrial_Activity_Category"].value_counts().reset_index()
            activity_counts.columns = ["Activity Category", "Count"]
            fig_activities = px.pie(activity_counts, names="Activity Category", values="Count", hole=0.4,
                                    color_discrete_sequence=px.colors.qualitative.Pastel)
            st.plotly_chart(fig_activities, use_container_width=True)

        # Requirement 5: Consent Duration Distribution Map
        with chart_row1_right:
            st.subheader("Distribution of Consent Durations")
            duration_counts = df['Consent Duration (Bins)'].value_counts().reindex(duration_labels).reset_index()
            duration_counts.columns = ["Duration", "Count"]
            fig_durations = px.bar(duration_counts, x="Duration", y="Count", color="Duration", text="Count",
                                   color_discrete_sequence=px.colors.qualitative.Safe)
            fig_durations.update_layout(showlegend=False)
            st.plotly_chart(fig_durations, use_container_width=True)

        chart_row2_left, chart_row2_right = st.columns(2)

        # Requirement 1 & 4: Ranked Infringements Based on Unitary Plan Rule Codes
        with chart_row2_left:
            st.subheader("Most Frequently Breached E14 Rules")
            exploded_rules_df = df.explode('AUP_E14_Rules_Infringed').dropna(subset=['AUP_E14_Rules_Infringed'])
            if not exploded_rules_df.empty and isinstance(exploded_rules_df['AUP_E14_Rules_Infringed'].iloc[0], dict):
                rules_expanded = pd.json_normalize(exploded_rules_df['AUP_E14_Rules_Infringed'])
                rule_counts = rules_expanded.groupby(['rule', 'activity_status']).size().reset_index(name='Count')
                rule_counts = rule_counts.sort_values(by="Count", ascending=True)
                
                fig_rules = px.bar(rule_counts, x="Count", y="rule", color="activity_status", 
                                   orientation='h', labels={"rule": "AUP E14 Rule Reference", "activity_status": "Activity Type Status"})
                st.plotly_chart(fig_rules, use_container_width=True)
            else:
                st.info("No explicit structured E14 rules extracted from current document arrays.")

        # Requirement 3: Categorization of Extraction Mitigation Measures
        with chart_row2_right:
            st.subheader("Common Industrial Mitigation Measures")
            exploded_mitigations_df = df.explode('Mitigation_Measures').dropna(subset=['Mitigation_Measures'])
            if not exploded_mitigations_df.empty:
                exploded_mitigations_df = exploded_mitigations_df[exploded_mitigations_df['Mitigation_Measures'] != "Not specified"]
                mitigation_counts = exploded_mitigations_df['Mitigation_Measures'].value_counts().head(10).reset_index()
                mitigation_counts.columns = ["Mitigation Measure", "Count"]
                
                fig_mitigation = px.bar(mitigation_counts, x="Count", y="Mitigation Measure", orientation='h',
                                        color="Count", color_continuous_scale="Blues")
                fig_mitigation.update_layout(yaxis={'categoryorder':'total ascending'}, coloraxis_showscale=False)
                st.plotly_chart(fig_mitigation, use_container_width=True)
            else:
                st.info("No discrete mechanical mitigation elements detected in conditions blocks.")

        st.markdown("---")

        # Interactive Data Filter Frame
        with st.expander("Consent Table & Custom Export Subsystems", expanded=True):
            display_columns = [
                "__file_name__", "Resource Consent Numbers", "Company Name", 
                "Address", "Industrial_Activity_Category", "Consent Status"
            ]
            display_df = df[display_columns].rename(columns={"__file_name__": "File Name"})
            st.dataframe(display_df)
            
            csv_output = df.drop(columns=["__file_bytes__", "Text Blob"]).to_csv(index=False).encode("utf-8")
            st.download_button("Download Processed Capstone Metrics (CSV)", csv_output, "processed_consents.csv", "text/csv")

        # Map Plotting Layer
        with st.expander("Geospatial Spatial Plots", expanded=False):
            map_clean_df = df.dropna(subset=["Latitude", "Longitude"])
            if not map_clean_df.empty:
                fig_map = px.scatter_mapbox(map_clean_df, lat="Latitude", lon="Longitude", hover_name="Company Name",
                                            hover_data={"Industrial_Activity_Category": True, "Consent Status": True},
                                            zoom=9, color="Consent Status", color_discrete_map={"Active":"green", "Expired":"red"})
                fig_map.update_layout(mapbox_style="open-street-map", margin={"r":0,"t":0,"l":0,"b":0})
                st.plotly_chart(fig_map, use_container_width=True)

        my_bar.progress(100, text="Dashboard Assets Configured!")
        time.sleep(1)
        my_bar.empty()

    else:
        my_bar.empty()
        st.warning("Data framework resolution failed. Verify file parameters.")

# --- Ask AI Context Retrieval Frame ---
st.markdown("---")
st.subheader("Ask AI About Consents")

with st.expander("AI Data Chatbot Contextual Engine", expanded=True):
    llm_provider = st.radio("Choose LLM Provider Mapping:", ["Gemini AI", "Groq AI"], horizontal=True)
    chat_input = st.text_area("Inquire about conditions, activity groupings, or statutory rules:")

    if st.button("Query Database"):
        if not chat_input.strip():
            st.warning("Provide text inputs to initiate parsing queries.")
        else:
            with st.spinner("Parsing data vectors..."):
                try:
                    if not df.empty:
                        context_df = df.drop(columns=["__file_bytes__", "Text Blob"]).copy()
                        if 'Issue Date' in context_df.columns and pd.api.types.is_datetime64_any_dtype(context_df['Issue Date']):
                            context_df['Issue Date'] = context_df['Issue Date'].dt.strftime('%Y-%m-%d')
                        if 'Expiry Date' in context_df.columns and pd.api.types.is_datetime64_any_dtype(context_df['Expiry Date']):
                            context_df['Expiry Date'] = context_df['Expiry Date'].dt.strftime('%Y-%m-%d')
                        context_sample_list = context_df.to_dict(orient="records")
                    else:
                        context_sample_list = [{"Status": "No internal records configured"}]

                    context_json = json.dumps(context_sample_list, indent=2)

                    system_message = f"""
                    You are an intelligent assistant analyzing Auckland Air Discharge Consents. 
                    Answer the User Query strictly based on the Provided JSON Data. 
                    If asked to count or list items, analyze the entire dataset provided and list the Company Names.
                    
                    Provided JSON Data:
                    {context_json}
                    """

                    answer = ""
                    if llm_provider == "Gemini AI" and google_api_key:
                        model = genai.GenerativeModel("gemini-2.0-flash")
                        response = model.generate_content(f"{system_message}\n\nUser Query: {chat_input}")
                        answer = response.text
                    elif llm_provider == "Groq AI" and groq_api_key:
                        chat_groq = ChatGroq(groq_api_key=groq_api_key, model_name="llama3-8b-8192")
                        response = chat_groq.invoke([SystemMessage(content=system_message), HumanMessage(content=chat_input)])
                        answer = response.content
                    else:
                        answer = "Target infrastructure unreachable. Validate environmental variable definitions."

                    st.markdown(f"### 🖥️ Response Interface\n{answer}")
                    log_ai_chat(chat_input, answer)

                except Exception as e:
                    st.error(f"Execution handling failure: {e}")

st.caption("Auckland Air Discharge Intelligence | Structural Analytics Deployment Platform")
