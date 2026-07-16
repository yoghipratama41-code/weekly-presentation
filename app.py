import io
import time
import streamlit as st
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
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

# Rate konversi RM -> SGD (fixed, sesuai referensi yang dikasih user)
RM_TO_SGD_RATE = 0.2850  # 1 RM = 0.2850 SGD (adjust as needed)

# Warna zona Weekly Spending (dipakai untuk background cell Remark & warna teks Surplus/Deficit)
SAFE_ZONE_COLOR = {"red": 0.0, "green": 0.0, "blue": 1.0}    # biru, sesuai "Safe Zone"
DANGER_ZONE_COLOR = {"red": 1.0, "green": 0.0, "blue": 0.0}  # merah, sesuai "Danger Zone"


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


# ============== HELPER: BROADCAST SUMMARY IMAGE PLACEMENT ==============
def _get_shape_full_text(page_element):
    """Gabungin semua textRun di dalam sebuah shape jadi satu string (strip whitespace)."""
    shape = page_element.get("shape")
    if not shape:
        return ""
    text_elements = shape.get("text", {}).get("textElements", [])
    full_text = "".join(
        te.get("textRun", {}).get("content", "") for te in text_elements
    )
    return full_text.strip()


def _find_placeholder_shapes(presentation, placeholders):
    """Cari shape yang isinya persis salah satu dari daftar placeholder (misal '{BroadM1}').
    Return dict: placeholder -> {page_object_id, object_id, size, transform}"""
    found = {}
    for slide in presentation.get("slides", []):
        page_object_id = slide.get("objectId")
        for el in slide.get("pageElements", []):
            text = _get_shape_full_text(el)
            if text in placeholders:
                found[text] = {
                    "page_object_id": page_object_id,
                    "object_id": el.get("objectId"),
                    "size": el.get("size"),
                    "transform": el.get("transform"),
                }
    return found


# ============== HELPER: WEEKLY SPENDING TABLE (RM/SGD, warna zona) ==============
def _get_table_cell_text(table_cell):
    """Gabungin semua textRun di dalam satu cell tabel jadi satu string (strip whitespace)."""
    text_elements = table_cell.get("text", {}).get("textElements", [])
    full_text = "".join(
        te.get("textRun", {}).get("content", "") for te in text_elements
    )
    return full_text.strip()


def _find_table_cells(presentation, placeholders):
    """Cari cell tabel yang isinya persis salah satu placeholder (misal '{SurplusM}').
    HARUS dipanggil SEBELUM replaceAllText mengganti teksnya, karena pencarian berdasarkan
    teks placeholder yang masih asli. Return dict: placeholder -> {table_object_id, row_index, column_index}"""
    found = {}
    for slide in presentation.get("slides", []):
        for el in slide.get("pageElements", []):
            table = el.get("table")
            if not table:
                continue
            table_object_id = el.get("objectId")
            for r, row in enumerate(table.get("tableRows", [])):
                for c, cell in enumerate(row.get("tableCells", [])):
                    text = _get_table_cell_text(cell)
                    if text in placeholders:
                        found[text] = {
                            "table_object_id": table_object_id,
                            "row_index": r,
                            "column_index": c,
                        }
    return found


def _build_spending_color_requests(cell_map, region_prefix, is_safe):
    """Bangun request buat ganti warna teks Surplus/Deficit & background cell Remark,
    sesuai zona (Safe = biru, Danger = merah)."""
    color = SAFE_ZONE_COLOR if is_safe else DANGER_ZONE_COLOR
    surplus_key = "{Surplus" + region_prefix + "}"
    remark_key = "{Remark" + region_prefix + "}"
    requests = []

    if surplus_key in cell_map:
        c = cell_map[surplus_key]
        requests.append({
            "updateTextStyle": {
                "objectId": c["table_object_id"],
                "cellLocation": {"rowIndex": c["row_index"], "columnIndex": c["column_index"]},
                "style": {"foregroundColor": {"opaqueColor": {"rgbColor": color}}},
                "textRange": {"type": "ALL"},
                "fields": "foregroundColor",
            }
        })

    if remark_key in cell_map:
        c = cell_map[remark_key]
        requests.append({
            "updateTableCellProperties": {
                "objectId": c["table_object_id"],
                "tableRange": {
                    "location": {"rowIndex": c["row_index"], "columnIndex": c["column_index"]},
                    "rowSpan": 1,
                    "columnSpan": 1,
                },
                "tableCellProperties": {
                    "tableCellBackgroundFill": {"solidFill": {"color": {"rgbColor": color}}}
                },
                "fields": "tableCellBackgroundFill.solidFill.color",
            }
        })

    return requests


def _format_amount(value):
    """Format angka: kalau bulat tampil tanpa desimal (624), kalau nggak tampil dengan
    desimal secukupnya tanpa trailing zero (307.51, 279.2)."""
    if abs(value - round(value)) < 0.005:
        return f"{value:.0f}"
    s = f"{value:.2f}".rstrip("0").rstrip(".")
    return s


def _get_image_dimensions(uploaded_file):
    """Ambil ukuran asli gambar (width, height dalam pixel) pakai Pillow."""
    from PIL import Image
    img = Image.open(io.BytesIO(uploaded_file.getvalue()))
    return img.size  # (width, height)


def _fit_size_and_transform(shape_info, img_width_px, img_height_px):
    """Hitung ukuran & posisi baru supaya gambar 'contain' (fit, tanpa distorsi) di dalam
    kotak placeholder, bukan 'stretch' (dipaksa pas ukuran box sehingga rasio gambar berubah).
    Gambar akan di-center di dalam box placeholder."""
    box_size = shape_info["size"]
    box_transform = shape_info["transform"]

    box_w = box_size["width"]["magnitude"] * box_transform.get("scaleX", 1)
    box_h = box_size["height"]["magnitude"] * box_transform.get("scaleY", 1)

    img_ratio = img_width_px / img_height_px
    box_ratio = box_w / box_h

    if img_ratio > box_ratio:
        # Gambar lebih "lebar" dari box -> lebar mengikuti box, tinggi menyesuaikan
        new_w = box_w
        new_h = box_w / img_ratio
    else:
        # Gambar lebih "tinggi" dari box -> tinggi mengikuti box, lebar menyesuaikan
        new_h = box_h
        new_w = box_h * img_ratio

    box_left = box_transform.get("translateX", 0)
    box_top = box_transform.get("translateY", 0)
    new_left = box_left + (box_w - new_w) / 2
    new_top = box_top + (box_h - new_h) / 2

    unit = box_size["width"].get("unit", "EMU")
    transform_unit = box_transform.get("unit", "EMU")

    new_size = {
        "width": {"magnitude": new_w, "unit": unit},
        "height": {"magnitude": new_h, "unit": unit},
    }
    new_transform = {
        "scaleX": 1,
        "scaleY": 1,
        "translateX": new_left,
        "translateY": new_top,
        "unit": transform_unit,
    }
    return new_size, new_transform


def _upload_image_to_drive(drive_service, uploaded_file):
    """Upload file gambar (dari st.file_uploader) ke Drive punya app ini (scope drive.file cukup),
    lalu di-set 'anyone with link: reader' supaya bisa diakses server Slides API buat createImage.
    Return URL gambar yang bisa dipakai di request createImage."""
    media = MediaIoBaseUpload(
        io.BytesIO(uploaded_file.getvalue()),
        mimetype=uploaded_file.type or "image/png",
        resumable=False,
    )
    file = drive_service.files().create(
        body={"name": uploaded_file.name}, media_body=media, fields="id"
    ).execute()
    file_id = file.get("id")

    drive_service.permissions().create(
        fileId=file_id, body={"type": "anyone", "role": "reader"}
    ).execute()

    return f"https://drive.google.com/uc?export=view&id={file_id}"


def replace_placeholders_with_images(drive_service, slides_service, presentation_id, image_uploads, status_box):
    """image_uploads: dict placeholder -> UploadedFile (misal '{BroadM1}': <file>).
    Placeholder kosong (user belum upload) dilewati saja."""
    image_uploads = {k: v for k, v in image_uploads.items() if v is not None}
    if not image_uploads:
        return

    presentation = slides_service.presentations().get(presentationId=presentation_id).execute()
    shape_map = _find_placeholder_shapes(presentation, set(image_uploads.keys()))

    missing = [ph for ph in image_uploads if ph not in shape_map]
    if missing:
        status_box.warning(f"⚠️ Placeholder berikut tidak ditemukan di slide, dilewati: {', '.join(missing)}")

    requests = []
    for placeholder, uploaded_file in image_uploads.items():
        if placeholder not in shape_map:
            continue
        shape_info = shape_map[placeholder]

        img_w_px, img_h_px = _get_image_dimensions(uploaded_file)
        fitted_size, fitted_transform = _fit_size_and_transform(shape_info, img_w_px, img_h_px)

        image_url = _upload_image_to_drive(drive_service, uploaded_file)

        requests.append({
            "createImage": {
                "url": image_url,
                "elementProperties": {
                    "pageObjectId": shape_info["page_object_id"],
                    "size": fitted_size,
                    "transform": fitted_transform,
                },
            }
        })
        requests.append({"deleteObject": {"objectId": shape_info["object_id"]}})

    if requests:
        slides_service.presentations().batchUpdate(
            presentationId=presentation_id, body={"requests": requests}
        ).execute()
        status_box.success(f"🖼️ {len(image_uploads) - len(missing)} gambar Broadcast Summary berhasil ditempatkan!")


# ============== CORE AUTOMATION LOGIC ==============
def generate_weekly_report(creds, current_week, last_week, date_range, shopee_data,
                            broadcast_dates, broadcast_images, grab_metrics, spending_data, status_box):
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

    # 1b. Cari lokasi cell tabel Weekly Spending SEBELUM teksnya diganti
    #     (harus sebelum replaceAllText, karena pencarian berdasarkan teks placeholder asli)
    spending_placeholders = {
        "{BudgetCapM}", "{SpentM}", "{SurplusM}", "{RemarkM}",
        "{BudgetCapS}", "{SpentS}", "{SurplusS}", "{RemarkS}",
    }
    presentation_awal = slides_service.presentations().get(presentationId=id_slide_baru).execute()
    spending_cell_map = _find_table_cells(presentation_awal, spending_placeholders)

    # 1c. Hitung Weekly Spending (Malaysia: RM -> SGD, Singapore: langsung SGD)
    spending_data = spending_data or {}
    m_data = spending_data.get("M", {})
    s_data = spending_data.get("S", {})

    m_budget_cap = float(m_data.get("budget_cap") or 0)
    m_rm_spent = float(m_data.get("rm_spent") or 0)
    m_sgd_spent = m_rm_spent * RM_TO_SGD_RATE
    m_surplus = m_budget_cap - m_sgd_spent
    m_is_safe = m_surplus >= 0

    s_budget_cap = float(s_data.get("budget_cap") or 0)
    s_sgd_spent = float(s_data.get("sgd_spent") or 0)
    s_surplus = s_budget_cap - s_sgd_spent
    s_is_safe = s_surplus >= 0

    spending_text_values = {
        "{BudgetCapM}": f"S${_format_amount(m_budget_cap)}",
        "{SpentM}": f"S${_format_amount(m_sgd_spent)} (RM {_format_amount(m_rm_spent)})",
        "{SurplusM}": f"{'+' if m_is_safe else '-'}S${_format_amount(abs(m_surplus))}",
        "{RemarkM}": "Safe Zone" if m_is_safe else "Danger Zone",
        "{BudgetCapS}": f"S${_format_amount(s_budget_cap)}",
        "{SpentS}": f"S${_format_amount(s_sgd_spent)}",
        "{SurplusS}": f"{'+' if s_is_safe else '-'}S${_format_amount(abs(s_surplus))}",
        "{RemarkS}": "Safe Zone" if s_is_safe else "Danger Zone",
    }

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

        # Penggantian Tanggal Broadcast Summary (dipakai bareng untuk slide M dan slide S)
        {"replaceAllText": {"containsText": {"text": "{DatA}", "matchCase": True}, "replaceText": broadcast_dates["DatA"]}},
        {"replaceAllText": {"containsText": {"text": "{DatB}", "matchCase": True}, "replaceText": broadcast_dates["DatB"]}},
        {"replaceAllText": {"containsText": {"text": "{DatC}", "matchCase": True}, "replaceText": broadcast_dates["DatC"]}},
        {"replaceAllText": {"containsText": {"text": "{DatD}", "matchCase": True}, "replaceText": broadcast_dates["DatD"]}},
        {"replaceAllText": {"containsText": {"text": "{DatE}", "matchCase": True}, "replaceText": broadcast_dates["DatE"]}},
        
        # Penggantian Data Shopee (Slide 15)
        {"replaceAllText": {"containsText": {"text": "{{SHOPEE_PAYSLIPS}}", "matchCase": True}, "replaceText": shopee_data["payslips"]}},
        {"replaceAllText": {"containsText": {"text": "{{SHOPEE_RIDERS}}", "matchCase": True}, "replaceText": shopee_data["riders"]}},
        {"replaceAllText": {"containsText": {"text": "{{SHOPEE_ACCEPTED}}", "matchCase": True}, "replaceText": shopee_data["accepted"]}},
        {"replaceAllText": {"containsText": {"text": "{{SHOPEE_PERCENTAGE}}", "matchCase": True}, "replaceText": shopee_percentage_str}},
        {"replaceAllText": {"containsText": {"text": "{{SHOPEE_NOTES}}", "matchCase": True}, "replaceText": shopee_data["notes"]}},

        # Penggantian Metrik Payslips/Rider & Valid from Total
        # S = Grab Singapore, M = Grab Malaysia, G = Grab Timestamp Malaysia
        {"replaceAllText": {"containsText": {"text": "{PayslipsS}", "matchCase": True}, "replaceText": grab_metrics["PayslipsS"]}},
        {"replaceAllText": {"containsText": {"text": "{ValidS}", "matchCase": True}, "replaceText": grab_metrics["ValidS"]}},
        {"replaceAllText": {"containsText": {"text": "{PayslipsM}", "matchCase": True}, "replaceText": grab_metrics["PayslipsM"]}},
        {"replaceAllText": {"containsText": {"text": "{ValidM}", "matchCase": True}, "replaceText": grab_metrics["ValidM"]}},
        {"replaceAllText": {"containsText": {"text": "{PayslipsG}", "matchCase": True}, "replaceText": grab_metrics["PayslipsG"]}},
        {"replaceAllText": {"containsText": {"text": "{ValidG}", "matchCase": True}, "replaceText": grab_metrics["ValidG"]}},

        # Penggantian Weekly Spending (Malaysia & Singapore)
        {"replaceAllText": {"containsText": {"text": "{BudgetCapM}", "matchCase": True}, "replaceText": spending_text_values["{BudgetCapM}"]}},
        {"replaceAllText": {"containsText": {"text": "{SpentM}", "matchCase": True}, "replaceText": spending_text_values["{SpentM}"]}},
        {"replaceAllText": {"containsText": {"text": "{SurplusM}", "matchCase": True}, "replaceText": spending_text_values["{SurplusM}"]}},
        {"replaceAllText": {"containsText": {"text": "{RemarkM}", "matchCase": True}, "replaceText": spending_text_values["{RemarkM}"]}},
        {"replaceAllText": {"containsText": {"text": "{BudgetCapS}", "matchCase": True}, "replaceText": spending_text_values["{BudgetCapS}"]}},
        {"replaceAllText": {"containsText": {"text": "{SpentS}", "matchCase": True}, "replaceText": spending_text_values["{SpentS}"]}},
        {"replaceAllText": {"containsText": {"text": "{SurplusS}", "matchCase": True}, "replaceText": spending_text_values["{SurplusS}"]}},
        {"replaceAllText": {"containsText": {"text": "{RemarkS}", "matchCase": True}, "replaceText": spending_text_values["{RemarkS}"]}},
    ]

    slides_service.presentations().batchUpdate(
        presentationId=id_slide_baru, body={"requests": replace_requests}
    ).execute()

    # 3b. Ganti Warna Zona Weekly Spending (background Remark & warna teks Surplus/Deficit)
    status_box.info("🎨 Menyesuaikan warna zona Weekly Spending (Safe/Danger)...")
    color_requests = (
        _build_spending_color_requests(spending_cell_map, "M", m_is_safe)
        + _build_spending_color_requests(spending_cell_map, "S", s_is_safe)
    )
    if color_requests:
        slides_service.presentations().batchUpdate(
            presentationId=id_slide_baru, body={"requests": color_requests}
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

    # 5. Tempatkan Gambar Broadcast Summary (Malaysia & Singapore, dua slide terpisah)
    status_box.info("🖼️ Menempatkan gambar Broadcast Summary (Malaysia & Singapore)...")
    replace_placeholders_with_images(drive_service, slides_service, id_slide_baru, broadcast_images, status_box)

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

    st.divider()

    st.subheader("📊 Payslips/Rider & Valid from Total")
    st.caption("S = Grab Singapore, M = Grab Malaysia, G = Grab Timestamp Malaysia")
    gcol_s, gcol_m, gcol_g = st.columns(3)
    with gcol_s:
        st.markdown("**Grab Singapore (S)**")
        input_payslips_s = st.text_input("Payslips/Rider", value="20.85", key="payslips_s")
        input_valid_s = st.text_input("Valid from Total", value="90.8%", key="valid_s")
    with gcol_m:
        st.markdown("**Grab Malaysia (M)**")
        input_payslips_m = st.text_input("Payslips/Rider", value="20.85", key="payslips_m")
        input_valid_m = st.text_input("Valid from Total", value="90.8%", key="valid_m")
    with gcol_g:
        st.markdown("**Grab Timestamp Malaysia (G)**")
        input_payslips_g = st.text_input("Payslips/Rider", value="20.85", key="payslips_g")
        input_valid_g = st.text_input("Valid from Total", value="90.8%", key="valid_g")

    st.divider()

    st.subheader("💰 Weekly Spending")
    st.caption(f"Malaysia: input Ringgit yang di-spend, otomatis dikonversi ke SGD (rate: 1 RM = {RM_TO_SGD_RATE} SGD). Singapore: input SGD langsung.")
    wcol_m, wcol_s = st.columns(2)
    with wcol_m:
        st.markdown("**🇲🇾 Malaysia**")
        input_budget_cap_m = st.text_input("Weekly Budget Cap (SGD)", value="307.51", key="budget_cap_m")
        input_rm_spent_m = st.text_input("Amount Spent (RM)", value="879.5", key="rm_spent_m")
    with wcol_s:
        st.markdown("**🇸🇬 Singapore**")
        input_budget_cap_s = st.text_input("Weekly Budget Cap (SGD)", value="624", key="budget_cap_s")
        input_sgd_spent_s = st.text_input("Amount Spent (SGD)", value="646", key="sgd_spent_s")

    st.divider()

    st.subheader("📢 Broadcast Summary")
    st.caption("Tanggal di bawah ini dipakai bareng untuk slide Malaysia dan Singapore ({DatA}-{DatE}).")
    dcol1, dcol2, dcol3, dcol4, dcol5 = st.columns(5)
    with dcol1:
        input_dat_a = st.text_input("DatA", value="13th July")
    with dcol2:
        input_dat_b = st.text_input("DatB", value="14th July")
    with dcol3:
        input_dat_c = st.text_input("DatC", value="15th July")
    with dcol4:
        input_dat_d = st.text_input("DatD", value="16th July")
    with dcol5:
        input_dat_e = st.text_input("DatE", value="17th July")

    st.markdown("**🇲🇾 Broadcast Summary - Malaysia (slide terpisah)**")
    st.caption("Upload screenshot untuk tiap slot. Kosongkan kalau slot itu tidak dipakai minggu ini.")
    mcol1, mcol2, mcol3, mcol4, mcol5 = st.columns(5)
    with mcol1:
        img_broad_m1 = st.file_uploader("BroadM1", type=["png", "jpg", "jpeg"], key="broad_m1")
    with mcol2:
        img_broad_m2 = st.file_uploader("BroadM2", type=["png", "jpg", "jpeg"], key="broad_m2")
    with mcol3:
        img_broad_m3 = st.file_uploader("BroadM3", type=["png", "jpg", "jpeg"], key="broad_m3")
    with mcol4:
        img_broad_m4 = st.file_uploader("BroadM4", type=["png", "jpg", "jpeg"], key="broad_m4")
    with mcol5:
        img_broad_m5 = st.file_uploader("BroadM5", type=["png", "jpg", "jpeg"], key="broad_m5")

    st.markdown("**🇸🇬 Broadcast Summary - Singapore (slide terpisah)**")
    scol1, scol2, scol3, scol4, scol5 = st.columns(5)
    with scol1:
        img_broad_s1 = st.file_uploader("BroadS1", type=["png", "jpg", "jpeg"], key="broad_s1")
    with scol2:
        img_broad_s2 = st.file_uploader("BroadS2", type=["png", "jpg", "jpeg"], key="broad_s2")
    with scol3:
        img_broad_s3 = st.file_uploader("BroadS3", type=["png", "jpg", "jpeg"], key="broad_s3")
    with scol4:
        img_broad_s4 = st.file_uploader("BroadS4", type=["png", "jpg", "jpeg"], key="broad_s4")
    with scol5:
        img_broad_s5 = st.file_uploader("BroadS5", type=["png", "jpg", "jpeg"], key="broad_s5")

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

    # Tanggal Broadcast Summary (dipakai bareng untuk slide M dan S)
    broadcast_dates = {
        "DatA": input_dat_a.strftime("%d/%m/%Y"),
        "DatB": input_dat_b.strftime("%d/%m/%Y"),
        "DatC": input_dat_c.strftime("%d/%m/%Y"),
        "DatD": input_dat_d.strftime("%d/%m/%Y"),
        "DatE": input_dat_e.strftime("%d/%m/%Y"),
    }

    # Metrik Payslips/Rider & Valid from Total (S=Grab Singapore, M=Grab Malaysia, G=Grab Timestamp Malaysia)
    grab_metrics = {
        "PayslipsS": input_payslips_s,
        "ValidS": input_valid_s,
        "PayslipsM": input_payslips_m,
        "ValidM": input_valid_m,
        "PayslipsG": input_payslips_g,
        "ValidG": input_valid_g,
    }

    # Gambar Broadcast Summary per slot placeholder (nilai None kalau belum diupload, akan dilewati)
    broadcast_images = {
        "{BroadM1}": img_broad_m1,
        "{BroadM2}": img_broad_m2,
        "{BroadM3}": img_broad_m3,
        "{BroadM4}": img_broad_m4,
        "{BroadM5}": img_broad_m5,
        "{BroadS1}": img_broad_s1,
        "{BroadS2}": img_broad_s2,
        "{BroadS3}": img_broad_s3,
        "{BroadS4}": img_broad_s4,
        "{BroadS5}": img_broad_s5,
    }
    
    # Weekly Spending (Malaysia: RM -> SGD, Singapore: langsung SGD)
    spending_data = {
        "M": {"budget_cap": input_budget_cap_m, "rm_spent": input_rm_spent_m},
        "S": {"budget_cap": input_budget_cap_s, "sgd_spent": input_sgd_spent_s},
    }

    with st.spinner("Memproses laporan mingguan..."):
        try:
            hasil_link = generate_weekly_report(
                creds, 
                input_current_week, 
                input_last_week, 
                input_date_range, 
                shopee_data,
                broadcast_dates,
                broadcast_images,
                grab_metrics,
                spending_data,
                status_box
            )
            st.balloons()
            st.success("🎉 Weekly Report berhasil dibuat!")
            st.markdown(f"### 👉 **[Buka Laporan Mingguan Anda Di Sini]({hasil_link})**")
        except Exception as e:
            st.error(f"Terjadi kesalahan saat memproses laporan: {e}")
