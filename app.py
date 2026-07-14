import time
import streamlit as st
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest

# ============== KONFIGURASI ==============
st.set_page_config(page_title="GNS Weekly Report Auto", page_icon="📈", layout="centered")

# ID Template Presentasi Weekly Report
TEMPLATE_PRESENTATION_ID = "1paRGPc-qda5c0Ugrg6yI7U3xUVcga7lF4wCFQKCJqw0" 

# Kredensial dari Streamlit Secrets — SAMA seperti yang dipakai di app Endweek
GOOGLE_CLIENT_ID = st.secrets["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = st.secrets["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = st.secrets["GOOGLE_REFRESH_TOKEN"]

# Scope disamakan persis dengan app Endweek (drive.file, drive.readonly, presentations).
# drive.readonly sudah cukup untuk refreshSheetsChart, jadi spreadsheets.readonly TIDAK perlu
# ditambahkan — itu yang kemarin bikin invalid_scope karena refresh token lama belum punya izin itu.
SCOPES = (
    "https://www.googleapis.com/auth/drive.file "
    "https://www.googleapis.com/auth/drive.readonly "
    "https://www.googleapis.com/auth/presentations"
)
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


# ============== AUTHENTICATION ==============
@st.cache_resource(ttl=1800)
def get_creds():
    """Mengambil kredensial menggunakan Refresh Token tanpa perlu login interaktif berulang.
    Pakai secrets.toml yang SAMA dengan app Endweek — scope-nya identik jadi tidak perlu
    generate refresh token baru."""
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri=TOKEN_ENDPOINT,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES.split(),
    )
    creds.refresh(GoogleAuthRequest())
    return creds


# ============== CORE AUTOMATION LOGIC ==============
def generate_weekly_report(creds, current_week, last_week, date_range, shopee_data, status_box):
    drive_service = build("drive", "v3", credentials=creds)
    slides_service = build("slides", "v1", credentials=creds)

    # 1. Duplikasi Template
    nama_slide_baru = f"Weekly Report - Week {current_week} ({time.strftime('%Y-%m-%d %H:%M')})"
    status_box.info("📋 Menduplikasi template presentasi...")
    copy = drive_service.files().copy(
        fileId=TEMPLATE_PRESENTATION_ID, body={"name": nama_slide_baru}
    ).execute()
    id_slide_baru = copy.get("id")
    link_presentasi = f"https://docs.google.com/presentation/d/{id_slide_baru}/edit"

    # 2. Hitung Persentase Shopee Accepted
    try:
        shopee_payslips_val = float(shopee_data["payslips"])
        shopee_accepted_val = float(shopee_data["accepted"])
        if shopee_payslips_val > 0:
            shopee_percentage = (shopee_accepted_val / shopee_payslips_val) * 100
            # Format 1 angka di belakang koma (contoh: 84.8)
            shopee_percentage_str = f"{shopee_percentage:.1f}" 
        else:
            shopee_percentage_str = "0.0"
    except ValueError:
        shopee_percentage_str = "0.0"

    # 3. Ganti Teks (Placeholder)
    status_box.info("✏️ Menulis ulang data teks di seluruh slide (Waktu & Shopee)...")
    replace_requests = [
        {"replaceAllText": {"containsText": {"text": "{{CURRENT_WEEK}}", "matchCase": True}, "replaceText": current_week}},
        {"replaceAllText": {"containsText": {"text": "{{LAST_WEEK}}", "matchCase": True}, "replaceText": last_week}},
        {"replaceAllText": {"containsText": {"text": "{{DATE_RANGE}}", "matchCase": True}, "replaceText": date_range}},
        
        # Penggantian Data Shopee (Slide 15)
        {"replaceAllText": {"containsText": {"text": "{{SHOPEE_PAYSLIPS}}", "matchCase": True}, "replaceText": shopee_data["payslips"]}},
        {"replaceAllText": {"containsText": {"text": "{{SHOPEE_RIDERS}}", "matchCase": True}, "replaceText": shopee_data["riders"]}},
        {"replaceAllText": {"containsText": {"text": "{{SHOPEE_ACCEPTED}}", "matchCase": True}, "replaceText": shopee_data["accepted"]}},
        {"replaceAllText": {"containsText": {"text": "{{SHOPEE_PERCENTAGE}}", "matchCase": True}, "replaceText": shopee_percentage_str}},
        {"replaceAllText": {"containsText": {"text": "{{SHOPEE_NOTES}}", "matchCase": True}, "replaceText": shopee_data["notes"]}},
    ]
    
    slides_service.presentations().batchUpdate(
        presentationId=id_slide_baru, body={"requests": replace_requests}
    ).execute()

    # 4. Refresh Chart & Tabel Tertaut
    status_box.info("🔄 Memindai dan memperbarui visualisasi (grafik & tabel) dari Google Sheets...")
    presentation = slides_service.presentations().get(presentationId=id_slide_baru).execute()
    refresh_requests = []

    for slide in presentation.get("slides", []):
        for el in slide.get("pageElements", []):
            if "sheetsChart" in el:
                refresh_requests.append({
                    "refreshSheetsChart": {
                        "objectId": el["objectId"]
                    }
                })
    
    if refresh_requests:
        slides_service.presentations().batchUpdate(
            presentationId=id_slide_baru,
            body={"requests": refresh_requests}
        ).execute()
        status_box.success(f"✅ {len(refresh_requests)} grafik/tabel berhasil diperbarui dengan data terkini!")
    else:
        status_box.warning("ℹ️ Tidak ada grafik/tabel tertaut yang ditemukan untuk di-refresh.")

    return link_presentasi


# ============== UI STREAMLIT ==============
st.title("📈 Auto-Generate Weekly Report")
st.write("Isi formulir di bawah untuk men-generate presentasi Weekly Report dengan data terbaru.")

# Coba Autentikasi
try:
    creds = get_creds()
except Exception as e:
    st.error(f"Gagal autentikasi ke Google: {e}")
    st.stop()

# Formulir Input Data
with st.form("form_report"):
    st.subheader("📅 Info Waktu")
    col1, col2 = st.columns(2)
    with col1:
        input_current_week = st.text_input("Minggu Ini (Contoh: 272)", value="272")
        input_last_week = st.text_input("Minggu Lalu (Contoh: 271)", value="271")
    with col2:
        input_date_range = st.text_input("Rentang Tanggal (Contoh: July 7th - July 13th, 2026)", value="July 7th - July 13th, 2026")
    
    st.divider()
    
    st.subheader("🛒 Info Shopee Malaysia (Slide 15)")
    col3, col4, col5 = st.columns(3)
    with col3:
        input_shopee_payslips = st.text_input("Total Payslips Collected", value="86")
    with col4:
        input_shopee_accepted = st.text_input("Total Accepted", value="73")
    with col5:
        input_shopee_riders = st.text_input("Riders Sharing", value="5")
        
    input_shopee_notes = st.text_area(
        "Notes (Potential Riders)", 
        value="- 8765895426 (Fulfill the quota)\n- 1678116460 (Fulfill the quota)\n- 5361514471 (Fulfill the quota)",
        height=100
    )
    
    st.info("💡 Persentase (Accepted Percentage) akan dihitung otomatis oleh sistem.")
    st.caption("Pastikan template Google Slides Anda sudah memuat placeholder: {{CURRENT_WEEK}}, {{LAST_WEEK}}, {{DATE_RANGE}}, {{SHOPEE_PAYSLIPS}}, {{SHOPEE_RIDERS}}, {{SHOPEE_ACCEPTED}}, {{SHOPEE_PERCENTAGE}}, dan {{SHOPEE_NOTES}}.")
    
    submitted = st.form_submit_button("🚀 Generate Report", type="primary")

# Eksekusi saat tombol ditekan
if submitted:
    status_box = st.empty()
    
    # Bungkus data Shopee ke dalam dictionary
    shopee_data = {
        "payslips": input_shopee_payslips,
        "accepted": input_shopee_accepted,
        "riders": input_shopee_riders,
        "notes": input_shopee_notes
    }
    
    with st.spinner("Memproses laporan mingguan..."):
        try:
            hasil_link = generate_weekly_report(
                creds, 
                input_current_week, 
                input_last_week, 
                input_date_range, 
                shopee_data,
                status_box
            )
            st.balloons()
            st.success("🎉 Weekly Report berhasil dibuat!")
            st.markdown(f"### 👉 **[Buka Laporan Mingguan Anda Di Sini]({hasil_link})**")
        except Exception as e:
            st.error(f"Terjadi kesalahan saat memproses laporan: {e}")
