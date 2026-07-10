import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import logging
import re
import io
import requests

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- SET CONFIGURATION ---
st.set_page_config(page_title="XTB Dashboard", page_icon="💰", layout="wide")

st.title("💰 Prywatny Dashboard Finansowy XTB")
st.markdown("---")

# --- BACKEND LOGIC ---
def parse_xtb_comment(row: pd.Series) -> tuple:
    comment = row.get("Comment")
    tx_type = row.get("Type")
    if not isinstance(comment, str): return None, None
    pattern = r"(?:OPEN|CLOSE)\s+(?:BUY|SELL)\s+([\d.]+)(?:/[\d.]+)?\s+@\s+([\d.]+)"
    match = re.search(pattern, comment)
    if match:
        volume = float(match.group(1))
        price = float(match.group(2))
        if tx_type == "Stock purchase": return volume, price
        elif tx_type == "Stock sell": return -volume, price
    return None, None

def calculate_accurate_portfolio(df: pd.DataFrame) -> tuple:
    asset_types = ["Stock purchase", "Stock sell"]
    trade_df = df[df["Type"].isin(asset_types)].copy()
    if trade_df.empty: 
        return pd.DataFrame(), 0.0
    
    parsed_data = trade_df.apply(parse_xtb_comment, axis=1)
    trade_df["Volume_Adjusted"] = [x[0] if x is not None else None for x in parsed_data]
    trade_df["Price"] = [x[1] if x is not None else None for x in parsed_data]
    
    # Wyliczanie obecnego stanu posiadania i uśrednionej ceny zakupu na podstawie transakcji
    portfolio = trade_df.groupby("Ticker").agg(
        Shares_Owned=("Volume_Adjusted", "sum"),
        Net_Cash_Flow=("Amount", "sum")
    ).reset_index()
    portfolio["Shares_Owned"] = portfolio["Shares_Owned"].round(6)
    active_portfolio = portfolio[portfolio["Shares_Owned"] > 0].copy()
    
    # Wyliczamy wartość rynkową na bazie ceny zakupu jako bezpieczny fallback (gdy brak Yahoo)
    # Dla uproszczenia bierzemy ostatnią cenę zakupu z transakcji jako Current_Price
    last_prices = {}
    for ticker in active_portfolio["Ticker"]:
        ticker_trades = trade_df[trade_df["Ticker"] == ticker]
        if not ticker_trades.empty:
            last_prices[ticker] = ticker_trades.iloc[-1]["Price"]
            
    active_portfolio["Current_Price"] = active_portfolio["Ticker"].map(last_prices)
    # Z pliku XTB wartość w PLN wyciągamy bezpośrednio z ujemnego Net_Cash_Flow (włożony kapitał)
    active_portfolio["Current_Value_PLN"] = -active_portfolio["Net_Cash_Flow"]
    active_portfolio["Asset_Currency"] = "PLN" # Wartość końcowa w raporcie XTB jest już w PLN
    
    realized_df = portfolio[portfolio["Shares_Owned"] <= 0].copy()
    realized_pnl = realized_df["Net_Cash_Flow"].sum() if not realized_df.empty else 0.0
    
    for idx, row in portfolio[portfolio["Shares_Owned"] > 0].iterrows():
        if row["Net_Cash_Flow"] > 0:
            realized_pnl += row["Net_Cash_Flow"]

    return active_portfolio, realized_pnl

def calculate_cash_stats(df: pd.DataFrame) -> dict:
    deposits = df[df["Type"].isin(["Deposit", "Transfer"])]["Amount"].sum()
    div_gross = df[df["Type"] == "Dividend"]["Amount"].sum()
    wht_tax = df[df["Type"] == "Withholding tax"]["Amount"].sum()
    interest = df[df["Type"] == "Free funds interest"]["Amount"].sum()
    interest_tax = df[df["Type"] == "Free funds interest tax"]["Amount"].sum()
    return {
        "deposits": deposits,
        "dividends": div_gross + wht_tax,
        "interest": interest + interest_tax
    }

def generate_stable_timeline(df: pd.DataFrame) -> pd.DataFrame:
    """Generates a day-by-day timeline using total cumulative asset values from ledger."""
    df_sorted = df.copy()
    df_sorted["Time"] = pd.to_datetime(df_sorted["Time"]).dt.date
    df_sorted = df_sorted.sort_values(by="Time")
    
    start_date = df_sorted["Time"].min()
    end_date = df_sorted["Time"].max()
    all_days = pd.date_range(start=start_date, end=end_date).date
    
    asset_types = ["Stock purchase", "Stock sell"]
    trade_df = df_sorted[df_sorted["Type"].isin(asset_types)].copy()
    if not trade_df.empty:
        parsed_data = trade_df.apply(parse_xtb_comment, axis=1)
        trade_df["Volume_Adjusted"] = [x[0] if x is not None else None for x in parsed_data]
        trade_df["Price"] = [x[1] if x is not None else None for x in parsed_data]
    
    timeline_records = []
    
    for current_day in all_days:
        sub_df = df_sorted[df_sorted["Time"] <= current_day]
        
        # 1. Suma wpłat (Wkład rzeczywisty)
        total_deposits = sub_df[sub_df["Type"].isin(["Deposit", "Transfer"])]["Amount"].sum()
        
        # 2. Wolna gotówka na koncie w danym dniu
        cash_balance = sub_df["Amount"].sum()
        
        # 3. Wycena posiadanych akcji w danym dniu po cenie zakupu
        stock_value_pln = 0.0
        if not trade_df.empty:
            sub_trades = trade_df[trade_df["Time"] <= current_day]
            if not sub_trades.empty:
                # Sprawdzamy stan posiadania i uśredniony koszt dla każdej spółki na ten dzień
                for ticker, group in sub_trades.groupby("Ticker"):
                    shares = group["Volume_Adjusted"].sum()
                    if shares > 0:
                        # Szacujemy wartość na podstawie kwot transakcji z raportu (Amount)
                        # Suma ujemnych Amount to kapitał ulokowany w spółce
                        invested_in_ticker = -group["Amount"].sum()
                        if invested_in_ticker > 0:
                            stock_value_pln += invested_in_ticker

        # Całkowita wartość portfela to wolna gotówka + wartość posiadanych akcji
        total_portfolio_value = cash_balance + stock_value_pln
        
        timeline_records.append({
            "Time": current_day,
            "Wpłaty Rzeczywiste (Wkład)": total_deposits,
            "Wartość Portfela Księgowa": total_portfolio_value
        })
        
    return pd.DataFrame(timeline_records)

# --- UI EXECUTION FLOW ---
try:
    GOOGLE_DRIVE_FILE_ID = "1icRPA0GdmAXU-U-WF_65QD1RxAfSc8oH"
    DRIVE_DOWNLOAD_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_DRIVE_FILE_ID}/export?format=xlsx"

    with st.spinner("Przetwarzanie danych lokalnych portfela..."):
        response = requests.get(DRIVE_DOWNLOAD_URL, timeout=10)

    if response.status_code == 200:
        raw_df = pd.read_excel(io.BytesIO(response.content), skiprows=4)
        raw_df.columns = raw_df.columns.str.strip()
        clean_df = raw_df.dropna(subset=["ID"])

        # Obliczenia bazowe (100% z pliku, bez sieci)
        final_portfolio, realized_pnl = calculate_accurate_portfolio(clean_df)
        cash = calculate_cash_stats(clean_df)
        timeline_df = generate_stable_timeline(clean_df)

        total_value_stocks = final_portfolio["Current_Value_PLN"].sum() if not final_portfolio.empty else 0.0
        
        # Całkowity zysk (Wycena + zrealizowany + dywidendy) minus wkład
        total_gain_pln = (total_value_stocks + cash["dividends"] + cash["interest"] + realized_pnl) - cash["deposits"]
        roi = (total_gain_pln / cash["deposits"]) * 100 if cash["deposits"] > 0 else 0

        # --- PANEL METRYK ---
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Wycena Akcji (PLN)", f"{total_value_stocks:,.2f} zł")
        col2.metric("Suma Twoich Wpłat", f"{cash['deposits']:,.2f} zł")
        col3.metric("Zrealizowany Zysk 🟢", f"{realized_pnl:,.2f} zł")
        col4.metric("Dywidendy + Odsetki", f"{(cash['dividends'] + cash['interest']):,.2f} zł")
        col5.metric("Niezrealizowany Zysk / Strata", f"{total_gain_pln:,.2f} zł", delta=f"{roi:.2f}%")

        st.markdown("---")

        # --- WYKRES LINIOWY ---
        st.subheader("📈 Zmiana wartości portfela w czasie vs Twoje wpłaty")
        
        fig_line = go.Figure()
        fig_line.add_trace(go.Scatter(
            x=timeline_df["Time"], y=timeline_df["Wpłaty Rzeczywiste (Wkład)"],
            mode='lines', name='Wpłaty Rzeczywiste (Twój Wkład)',
            line=dict(color='rgba(150, 150, 150, 0.7)', width=2, dash='dash')
        ))
        fig_line.add_trace(go.Scatter(
            x=timeline_df["Time"], y=timeline_df["Wartość Portfela Księgowa"],
            mode='lines', name='Całkowita Wartość (Akcje + Gotówka)',
            line=dict(color='#2ca02c', width=3)
        ))
        
        fig_line.update_layout(
            hovermode="x unified",
            xaxis_title="Data",
            yaxis_title="Wartość (PLN)",
            template="plotly_white",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        st.plotly_chart(fig_line, use_container_width=True)

        st.markdown("---")

        # --- WYKRESY STRUKTURY ---
        chart_col1, chart_col2 = st.columns(2)
        with chart_col1:
            st.subheader("Struktura Portfela")
            if not final_portfolio.empty:
                fig_pie = px.pie(final_portfolio, values="Current_Value_PLN", names="Ticker", hole=0.4,
                                 color_discrete_sequence=px.colors.sequential.RdBu)
                st.plotly_chart(fig_pie, use_container_width=True)

        with chart_col2:
            st.subheader("Wartość Pozycji w PLN")
            if not final_portfolio.empty:
                fig_bar = px.bar(final_portfolio.sort_values(by="Current_Value_PLN", ascending=True), 
                                 x="Current_Value_PLN", y="Ticker", orientation="h",
                                 text_auto=",.2f", color="Current_Value_PLN",
                                 color_continuous_scale=px.colors.sequential.Viridis)
                st.plotly_chart(fig_bar, use_container_width=True)

        # --- TABELA ---
        st.subheader("📋 Szczegóły Twoich Pozycji")
        if not final_portfolio.empty:
            st.dataframe(final_portfolio[["Ticker", "Shares_Owned", "Asset_Currency", "Current_Price", "Current_Value_PLN"]], use_container_width=True)

    else:
        st.error(f"Błąd Dysku Google. Status: {response.status_code}")

except Exception as e:
    st.error("💥 Wystąpił błąd podczas generowania dashboardu.")
    st.exception(e)