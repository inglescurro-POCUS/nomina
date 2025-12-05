import streamlit as st
import pandas as pd
import datetime
import uuid
import json
import calendar
import os

# ==========================================
# 1. CONSTANTS & CONFIGURATION
# ==========================================

st.set_page_config(
    page_title="Calculadora Nóminas SAS",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Hardcoded holidays/special x2 dates (MM-DD)
# Hardcoded holidays/special x2 dates (MM-DD)
SPECIAL_MD = {'01-01', '01-06', '02-28', '10-07', '12-25'} # 10-07 added/verified

# Default "normal payroll" net amounts (approx)
TYPICAL_NORMAL_BY_MONTH = {
    '01': 1496.85, '02': 1534.05, '03': 1554.71, '04': 1575.86,
    '05': 1491.11, '06': 1377.95, '07': 1444.33, '08': 1451.42,
    '09': 1520.21, '10': 1557.66, '11': 1561.30, '12': 1545.01
}

# Shift Models
# (labor: horas ordinarias/laborables, fest: horas festivas, ca: continuidad asistencial, loca: localizada)
ACT_MODELS = {
    "TARDE":      {"ca": 5.0,  "labor": 0.0,  "fest": 0.0,  "loca": 0.0},
    "G_LJ":       {"ca": 5.0,  "labor": 12.0, "fest": 0.0,  "loca": 0.0},
    "G_VIERNES":  {"ca": 5.0,  "labor": 4.0,  "fest": 8.0,  "loca": 0.0},
    "G_SABADO":   {"ca": 0.0,  "labor": 0.0,  "fest": 24.0, "loca": 0.0},
    "G_DOMINGO":  {"ca": 0.0,  "labor": 8.0,  "fest": 16.0, "loca": 0.0},
    "REFUERZO":   {"ca": 7.5,  "labor": 0.0,  "fest": 0.0,  "loca": 10.0},
    "G_24_MIX":   {"ca": 5.0,  "labor": 8.0,  "fest": 16.0, "loca": 0.0}, # Calibrated for Oct 7
}

ACT_LABELS = {
    "TARDE": "Tarde (5h CA)",
    "G_LJ": "Guardia L-J (17h)",
    "G_VIERNES": "Guardia Viernes",
    "G_SABADO": "Guardia Sábado (24h)",
    "G_DOMINGO": "Guardia Domingo",
    "REFUERZO": "Refuerzo (CA+Loc)",
    "G_24_MIX": "G. 24h Mixta (8 Lab/16 Fest)"
}

# Base Config for 2025 (Updated with PDF calibrations)
DEFAULT_CONFIG_TEMPLATE = {
    "irpf": 0.35,  # 35% default
    "rates": {
        "labor": 27.07,
        "fest": 29.47,
        "ca": 47.11,
        "locaFactor": 0.5
    },
    "prodFija": 733.42,
    "bases": {
        "ccBaseWorker": 2151.30,
        "ccRate": 0.0483,
        "fpRate": 0.0010,
        "solidarity": [
            {"base": 490.95, "rate": 0.0077}, # Calibrated from PDF
            {"base": 1561.21, "rate": 0.0083} # Calibrated from PDF
        ]
    }
}

# Specific overrides known from original code
MONTHLY_OVERRIDES = {
    "2025-03": {"bases": {"solidarity": [{"base": 490.95, "rate": 0.0015}, {"base": 614.30, "rate": 0.0017}]}},
    "2025-04": {"bases": {"solidarity": [{"base": 490.95, "rate": 0.0015}, {"base": 1169.64, "rate": 0.0017}]}},
    "2025-06": {"bases": {"ccBaseWorker": 2098.15, "solidarity": [{"base": 490.95, "rate": 0.0015}, {"base": 1354.80, "rate": 0.0017}]}},
}

# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================

def get_month_config(ym):
    """Retrieve config for a specific YYYY-MM, merging defaults with overrides."""
    # Start with a deep copy of the template
    cfg = json.loads(json.dumps(DEFAULT_CONFIG_TEMPLATE))
    
    # 1. Apply global user IRPF setting if set
    if 'global_irpf' in st.session_state:
        cfg['irpf'] = st.session_state['global_irpf'] / 100.0

    # 2. Apply hardcoded monthly overrides (if any specific logic differs)
    if ym in MONTHLY_OVERRIDES:
        # Deep merge simplistic
        over = MONTHLY_OVERRIDES[ym]
        if 'bases' in over:
            if 'ccBaseWorker' in over['bases']: cfg['bases']['ccBaseWorker'] = over['bases']['ccBaseWorker']
            if 'solidarity' in over['bases']: cfg['bases']['solidarity'] = over['bases']['solidarity']
            
    # 3. Apply user specific month overrides from session state
    if 'month_configs' in st.session_state and ym in st.session_state['month_configs']:
        user_cfg = st.session_state['month_configs'][ym]
        # Recursively update is complex, for now we assume user_cfg is a partial patch?
        # Simpler: if user has a config, it replaces the auto one OR we assume user inputs top-level fields
        # In this app, let's keep it simple: we store just a few editable fields per month
        if 'irpf' in user_cfg: cfg['irpf'] = user_cfg['irpf']
        if 'prodFija' in user_cfg: cfg['prodFija'] = user_cfg['prodFija']
        # We could add more overrides here if needed
        
    return cfg

def classify_date(d):
    """Determine guard type from date."""
    wd = d.weekday() # 0=Mon, 6=Sun
    if wd == 6: return "G_DOMINGO"
    if wd == 5: return "G_SABADO"
    if wd == 4: return "G_VIERNES"
    return "G_LJ"

def next_month_str(ym_str):
    """'2025-05' -> '2025-06'"""
    y, m = map(int, ym_str.split('-'))
    m += 1
    if m > 12:
        m = 1
        y += 1
    return f"{y}-{m:02d}"

def fmt_euro(val):
    return f"{val:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")

# ==========================================
# 3. STATE MANAGEMENT
# ==========================================

if 'acts' not in st.session_state:
    # Pre-loaded with User's October Data
    # Oct 3 (Fri), 7 (Tue Special), 15 (Wed), 20 (Mon), 27 (Mon), 30 (Thu Tarde)
    # Note: Oct 7 derived as 8h Lab + 16h Fest + 5h CA. 
    # We map this to a "Custom" entry or closest approximation. 
    # Since we don't have a perfect "Mixed 24h" model, we will use a workaround or new type?
    # For now, we'll map Oct 7 to "G_VIERNES" (4L/8F)?? No, that's hours undercount.
    # To match 48h Lab/16h Fest total, we need Oct 7 to provide 8h Lab + 16h Fest.
    # We will add a temporary "G_24_MIX" model just for this or handle it dynamically? 
    # Simpler: We'll inject it as a standard G_SABADO (24h) but knowing it might not perfectly match Lab hours unless we tweak.
    # WAIT: User wants "Optimized version". I will add "G_FEST_ESP_MIX" to models.
    
    st.session_state['acts'] = [
        {"id": str(uuid.uuid4()), "date": "2025-10-03", "type": "G_VIERNES", "special": False}, # Fri
        {"id": str(uuid.uuid4()), "date": "2025-10-07", "type": "G_24_MIX", "special": True},   # Tue Special x2
        {"id": str(uuid.uuid4()), "date": "2025-10-15", "type": "G_LJ", "special": False},      # Wed
        {"id": str(uuid.uuid4()), "date": "2025-10-20", "type": "G_LJ", "special": False},      # Mon
        {"id": str(uuid.uuid4()), "date": "2025-10-27", "type": "G_LJ", "special": False},      # Mon
        {"id": str(uuid.uuid4()), "date": "2025-10-30", "type": "TARDE", "special": False},     # Thu Tarde
    ]

if 'month_configs' not in st.session_state:
    st.session_state['month_configs'] = {}

if 'global_irpf' not in st.session_state:
    st.session_state['global_irpf'] = 35.0

if 'normal_overrides' not in st.session_state:
    st.session_state['normal_overrides'] = {}

# ==========================================
# 4. SIDEBAR
# ==========================================

with st.sidebar:
    st.title("Configuración")
    
    st.subheader("Globales")
    new_irpf = st.number_input("IRPF por defecto (%)", 0.0, 60.0, st.session_state['global_irpf'], step=0.5)
    if new_irpf != st.session_state['global_irpf']:
        st.session_state['global_irpf'] = new_irpf
        # Optionally force-update all existing month overrides? keeping it simple for now.

    st.divider()
    st.subheader("Datos / Backup")
    
    # Download
    data_export = {
        "acts": st.session_state['acts'],
        "month_configs": st.session_state['month_configs'],
        "normal_overrides": st.session_state['normal_overrides'],
        "global_irpf": st.session_state['global_irpf']
    }
    st.download_button(
        label="💾 Descargar copia seguridad (JSON)",
        data=json.dumps(data_export, indent=2, default=str),
        file_name=f"sas_nominas_backup_{datetime.date.today()}.json",
        mime="application/json"
    )
    
    # Upload
    uploaded_file = st.file_uploader("📂 Cargar copia seguridad", type=["json"])
    if uploaded_file is not None:
        try:
            data = json.load(uploaded_file)
            st.session_state['acts'] = data.get('acts', [])
            st.session_state['month_configs'] = data.get('month_configs', {})
            st.session_state['normal_overrides'] = data.get('normal_overrides', {})
            st.session_state['global_irpf'] = data.get('global_irpf', 35.0)
            st.success("Datos restaurados correctamente!")
            uploaded_file = None # Reset
        except Exception as e:
            st.error(f"Error al cargar archivo: {e}")

# ==========================================
# 5. MAIN UI
# ==========================================

st.title("Calculadora Nóminas SAS 🏥")
st.markdown("Calculadora de **complementarias** (guardias/tardes) y estimación de ingreso en banco.")

tabs = st.tabs(["👋 Gestión de Actos", "📊 Detalles / Mes", "💰 Nómina al Banco"])

# --- TAB 1: GESTIÓN ---
with tabs[0]:
    col1, col2 = st.columns([1, 1])
    
    with col1:
        st.subheader("Añadir Manual")
        with st.form("add_manual"):
            d_date = st.date_input("Fecha", datetime.date.today())
            d_type = st.selectbox("Tipo de Acto", options=list(ACT_MODELS.keys()), format_func=lambda x: ACT_LABELS[x])
            submitted = st.form_submit_button("Añadir Acto")
            if submitted:
                # Check special
                is_special = d_date.strftime("%m-%d") in SPECIAL_MD
                new_act = {
                    "id": str(uuid.uuid4()),
                    "date": d_date.strftime("%Y-%m-%d"),
                    "type": d_type,
                    "special": is_special
                }
                st.session_state['acts'].append(new_act)
                st.success(f"Añadido: {d_date} - {d_type}")
                st.rerun()

    with col2:
        st.subheader("Entrada Rápida")
        with st.form("add_auto"):
            q_month = st.date_input("Mes del calendario", datetime.date.today())
            q_days = st.text_input("Días (ej: 2, 8, 14, 25)")
            submitted_auto = st.form_submit_button("Generar Actos")
            if submitted_auto and q_days:
                days_list = [d.strip() for d in q_days.replace(';',',').split(',') if d.strip().isdigit()]
                count = 0
                for d in days_list:
                    try:
                        # Construct date
                        # q_month is a date object, we take year and month
                        target_date = datetime.date(q_month.year, q_month.month, int(d))
                        t_type = classify_date(target_date)
                        is_special = target_date.strftime("%m-%d") in SPECIAL_MD
                        st.session_state['acts'].append({
                            "id": str(uuid.uuid4()),
                            "date": target_date.strftime("%Y-%m-%d"),
                            "type": t_type,
                            "special": is_special
                        })
                        count += 1
                    except ValueError:
                        pass # Ignore invalid days
                st.success(f"Se han generado {count} actos automáticamente.")
                st.rerun()

    st.divider()
    with st.expander("ℹ️ Ver tabla oficial de distribución de horas"):
        if os.path.exists("distribucion hora.jpeg"):
            st.image("distribucion hora.jpeg", caption="Guía visual de tipos de guardia", use_column_width=True)
        else:
            st.warning("⚠️ No veo la imagen 'distribucion hora.jpeg'. Si estás en Streamlit Cloud, asegúrate de haber subido este archivo a tu GitHub.")
    
    st.subheader("Listado de Actos")
    
    if not st.session_state['acts']:
        st.info("No hay actos registrados.")
    else:
        # Show as simple table or dataframe with delete button
        # Streamlit creates a bit of friction for row-based actions like delete. 
        # Using a selectbox to delete is safer/easier than data_editor for now in basic mode.
        
        # We sort them first
        sorted_acts = sorted(st.session_state['acts'], key=lambda x: x['date'], reverse=True)
        
        # Create a display list
        disp_data = []
        for a in sorted_acts:
            disp_data.append({
                "Fecha": a['date'],
                "Tipo": ACT_LABELS.get(a['type'], a['type']),
                "Especial x2": "✅" if a['special'] else "❌",
                "ID": a['id'] # Hidden logic
            })
        
        df = pd.DataFrame(disp_data)
        st.dataframe(df[["Fecha", "Tipo", "Especial x2"]], use_container_width=True)
        
        with st.expander("Borrar actos"):
            to_del = st.selectbox("Selecciona acto para borrar", options=df["ID"].tolist(), format_func=lambda x: next((f"{d['Fecha']} - {d['Tipo']}" for d in disp_data if d['ID'] == x), x))
            if st.button("🗑️ Borrar Seleccionado"):
                st.session_state['acts'] = [x for x in st.session_state['acts'] if x['id'] != to_del]
                st.success("Borrado.")
                st.rerun()
            if st.button("💀 Borrar TODO (Peligro)"):
                st.session_state['acts'] = []
                st.rerun()

# --- AGGREGATION LOGIC ---
# We calculate everything on the fly to keep state simple
aggregated_by_month = {}
for act in st.session_state['acts']:
    ym = act['date'][:7] # YYYY-MM
    if ym not in aggregated_by_month:
        aggregated_by_month[ym] = {"acts": [], "hours": {"labor":0, "fest":0, "ca":0, "loca":0}}
    aggregated_by_month[ym]["acts"].append(act)
    
    m = ACT_MODELS[act['type']]
    aggregated_by_month[ym]["hours"]["labor"] += m["labor"]
    aggregated_by_month[ym]["hours"]["fest"] += m["fest"]
    aggregated_by_month[ym]["hours"]["ca"] += m["ca"]
    aggregated_by_month[ym]["hours"]["loca"] += m["loca"]

# Calculate financials per month
financials_by_month = {}
sorted_months = sorted(aggregated_by_month.keys())

for ym in sorted_months:
    data = aggregated_by_month[ym]
    cfg = get_month_config(ym)
    
    # Devengos
    dev024 = 0.0
    dev025 = 0.0
    dev180 = 0.0
    loca_rate_val = cfg["rates"]["labor"] * cfg["rates"]["locaFactor"]
    
    for a in data["acts"]:
        m = ACT_MODELS[a["type"]]
        factor = 2.0 if a["special"] else 1.0
        
        dev024 += (m["labor"] * cfg["rates"]["labor"] + m["loca"] * loca_rate_val) * factor
        dev025 += (m["fest"] * cfg["rates"]["fest"]) * factor
        dev180 += (m["ca"] * cfg["rates"]["ca"]) # CA no se multiplica por x2 en festivos especiales
        
    devPF = cfg["prodFija"]
    devTotal = dev024 + dev025 + dev180 + devPF
    
    # Descuentos
    ccBase = cfg["bases"]["ccBaseWorker"]
    cuotCC = ccBase * cfg["bases"]["ccRate"]
    cuotFP = ccBase * cfg["bases"]["fpRate"]
    
    cuotSol = 0.0
    for tramo in cfg["bases"]["solidarity"]:
        cuotSol += tramo["base"] * tramo["rate"]
        
    irpf_amount = devTotal * cfg["irpf"]
    total_descuentos = irpf_amount + cuotCC + cuotFP + cuotSol
    neto = devTotal - total_descuentos
    
    financials_by_month[ym] = {
        "hours": data["hours"],
        "dev": {"024": dev024, "025": dev025, "180": dev180, "PF": devPF, "Total": devTotal},
        "desc": {"IRPF": irpf_amount, "CC": cuotCC, "FP": cuotFP, "Sol": cuotSol, "Total": total_descuentos},
        "neto": neto,
        "cfg": cfg
    }


# --- TAB 2: DETALLES ---
with tabs[1]:
    if not financials_by_month:
        st.info("Sin datos para mostrar.")
    else:
        sel_month = st.selectbox("Selecciona Mes de Servicio", sorted_months, index=len(sorted_months)-1)
        res = financials_by_month[sel_month]
        
        st.markdown(f"### Resultados: {sel_month}")
        
        # Override Editor
        with st.expander("⚙️ Editar Configuración de este Mes"):
            curr_irpf = res["cfg"]["irpf"] * 100
            curr_pf = res["cfg"]["prodFija"]
            
            col_e1, col_e2 = st.columns(2)
            new_m_irpf = col_e1.number_input(f"IRPF {sel_month} (%)", 0.0, 60.0, curr_irpf, step=0.5, key=f"irpf_{sel_month}")
            new_m_pf = col_e2.number_input(f"Prod. Fija {sel_month} (€)", 0.0, 2000.0, float(curr_pf), step=10.0, key=f"pf_{sel_month}")
            
            if col_e1.button("Guardar cambios mes"):
                if sel_month not in st.session_state['month_configs']: st.session_state['month_configs'][sel_month] = {}
                st.session_state['month_configs'][sel_month]['irpf'] = new_m_irpf / 100.0
                st.session_state['month_configs'][sel_month]['prodFija'] = new_m_pf
                st.rerun()

        # Display Metrics
        col1, col2, col3 = st.columns(3)
        col1.metric("Horas Laborables", f"{res['hours']['labor']:.1f}")
        col2.metric("Horas Festivas", f"{res['hours']['fest']:.1f}")
        col3.metric("Continuidad Asistencial", f"{res['hours']['ca']:.1f}")
        
        st.markdown("#### Desglose Económico")
        d_df = pd.DataFrame([
            {"Concepto": "Jornada Compl. (024)", "Importe": fmt_euro(res['dev']['024'])},
            {"Concepto": "Jornada Festiva (025)", "Importe": fmt_euro(res['dev']['025'])},
            {"Concepto": "Cont. Asistencial (180)", "Importe": fmt_euro(res['dev']['180'])},
            {"Concepto": "Prod. Fija (215)", "Importe": fmt_euro(res['dev']['PF'])},
            {"Concepto": "--- TOTAL DEVENGADO ---", "Importe": fmt_euro(res['dev']['Total'])},
        ])
        st.table(d_df)
        
        desc_df = pd.DataFrame([
             {"Concepto": "IRPF", "Importe": fmt_euro(res['desc']['IRPF'])},
             {"Concepto": "Seguridad Social", "Importe": fmt_euro(res['desc']['CC'])},
             {"Concepto": "Solidaridad/MEI", "Importe": fmt_euro(res['desc']['Sol'])},
             {"Concepto": "--- TOTAL DESCUENTOS ---", "Importe": fmt_euro(res['desc']['Total'])},
        ])
        st.table(desc_df)
        
        st.success(f"**Líquido Complementaria: {fmt_euro(res['neto'])}**")

# --- TAB 3: BANCO ---
with tabs[2]:
    st.markdown("### Calendario de Pagos")
    st.info("La nómina de **Junio** se paga en **Julio**, etc. Aquí ves cuánto entra al banco.")
    
    # Calculate payments
    # Map Service Month -> Payment Month
    payments = {} # PayMonthStr -> {comp: float, from: str}
    
    for sm in sorted_months:
        pm = next_month_str(sm)
        if pm not in payments:
            payments[pm] = {"comp": 0.0, "from": []}
        payments[pm]["comp"] += financials_by_month[sm]["neto"]
        payments[pm]["from"].append(sm)
        
    payment_months = sorted(payments.keys())
    
    if not payment_months:
        st.warning("No hay datos suficientes para calcular pagos.")
    else:
        for pm in payment_months:
            st.markdown(f"#### 🗓️ Nómina de **{pm}**")
            
            comp_val = payments[pm]["comp"]
            from_str = ", ".join(payments[pm]["from"])
            
            # Normal payroll handling
            mm = pm.split('-')[1]
            typical_val = TYPICAL_NORMAL_BY_MONTH.get(mm, 1500.00)
            
            override_key = f"normal_{pm}"
            # Check if override exists
            current_override = st.session_state['normal_overrides'].get(pm, 0.0)
            
            col1, col2 = st.columns(2)
            with col1:
                use_manual = st.checkbox("Usar valor manual nómina normal", value=(current_override > 0), key=f"chk_{pm}")
                if use_manual:
                    normal_val = st.number_input("Líquido Normal Real (€)", value=float(current_override if current_override > 0 else typical_val), key=f"inp_{pm}")
                    if normal_val != current_override:
                        st.session_state['normal_overrides'][pm] = normal_val
                        # Rerun is annoying here inside loop, but needed for total sync. check if st.form better?
                        # We'll just trust the calc below for immediate display
                else:
                    normal_val = typical_val
                    if pm in st.session_state['normal_overrides']:
                        del st.session_state['normal_overrides'][pm]
            
            with col2:
                total = normal_val + comp_val
                st.metric("Total a recibir", fmt_euro(total), delta=f"Normal: {fmt_euro(normal_val)}")
                st.caption(f"Incluye complementaria de: {from_str} ({fmt_euro(comp_val)})")
                
            st.divider()
