import logging
import re
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# --- SET CONFIGURATION ---
st.set_page_config(page_title="XTB Dashboard", page_icon="💰", layout="wide")

st.title("💰 Prywatny Dashboard Finansowy XTB")
st.markdown("---")

# --- GOOGLE DRIVE CONFIGURATION ---
# Wklej tutaj ID swojego pliku wyciągnięte z linku Dysku Google
GOOGLE_DRIVE_FILE_ID = "1icRPA0GdmAXU-U-WF_65QD1RxAfSc8oH"

# Bezpośredni link formatujący download dla Google Drive
DRIVE_DOWNLOAD_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_DRIVE_FILE_ID}/export?format=xlsx"


# --- BACKEND LOGIC ---
def parse_xtb_comment(row: pd.Series) -> tuple:
    comment = row.get("Comment")
    tx_type = row.get("Type")
    if not isinstance(comment, str):
        return None, None
    pattern = (
        r"(?:OPEN|CLOSE)\s+(?:BUY|SELL)\s+([\d.]+)(?:/[\d.]+)?\s+@\s+([\d.]+)"
    )
    match = re.search(pattern, comment)
    if match:
        volume = float(match.group(1))
        price = float(match.group(2))
        if tx_type == "Stock purchase":
            return volume, price
        elif tx_type == "Stock sell":
            return -volume, price
    return None, None


def calculate_accurate_portfolio(df: pd.DataFrame) -> pd.DataFrame:
    asset_types = ["Stock purchase", "Stock sell"]
    trade_df = df[df["Type"].isin(asset_types)].copy()
    if trade_df.empty:
        return pd.DataFrame()
    parsed_data = trade_df.apply(parse_xtb_comment, axis=1)
    trade_df["Volume_Adjusted"] = [
        x[0] if x is not None else None for x in parsed_data
    ]
    trade_df["Price"] = [x[1] if x is not None else None for x in parsed_data]
    portfolio = (
        trade_df.groupby("Ticker")
        .agg(Shares_Owned=("Volume_Adjusted", "sum"), Net_Cash_Flow=("Amount", "sum"))
        .reset_index()
    )
    portfolio["Shares_Owned"] = portfolio["Shares_Owned"].round(6)
    active_portfolio = portfolio[portfolio["Shares_Owned"] > 0].copy()
    active_portfolio["Total_Invested_Raw"] = -active_portfolio["Net_Cash_Flow"]
    return active_portfolio.drop(columns=["Net_Cash_Flow"])


def fix_ticker_for_yahoo(xtb_ticker: str) -> str:
    if not isinstance(xtb_ticker, str):
        return xtb_ticker
    if xtb_ticker.endswith(".US"):
        return xtb_ticker.replace(".US", "")
    if xtb_ticker.endswith(".PL"):
        return xtb_ticker.replace(".PL", ".WA")
    if "." not in xtb_ticker:
        return f"{xtb_ticker}.WA"
    return xtb_ticker


@st.cache_data(ttl=3600)  # Odświeżaj ceny live co 1 godzinę
def fetch_market_and_fx_data(portfolio_df: pd.DataFrame):
    if portfolio_df.empty:
        return portfolio_df, {}
    updated_portfolio = portfolio_df.copy()
    updated_portfolio["Yahoo_Ticker"] = updated_portfolio["Ticker"].apply(
        fix_ticker_for_yahoo
    )
    ticker_list = updated_portfolio["Yahoo_Ticker"].tolist()

    tickers_data = yf.Tickers(" ".join(ticker_list))
    current_prices, currencies = {}, {}
    for y_ticker in ticker_list:
        try:
            info = tickers_data.tickers[y_ticker].info
            price = info.get("regularMarketPrice") or info.get("previousClose")
            currency = info.get("currency")
            if (
                price is not None
                and currency in ["ILA", "GBX"]
                and y_ticker.endswith(".WA")
            ):
                price /= 100.0
                currency = "PLN"
            current_prices[y_ticker] = price
            currencies[y_ticker] = currency
        except:
            current_prices[y_ticker], currencies[y_ticker] = None, None

    updated_portfolio["Current_Price"] = updated_portfolio["Yahoo_Ticker"].map(
        current_prices
    )
    updated_portfolio["Asset_Currency"] = updated_portfolio["Yahoo_Ticker"].map(
        currencies
    )
    updated_portfolio["Current_Value_Native"] = (
        updated_portfolio["Shares_Owned"] * updated_portfolio["Current_Price"]
    )

    unique_currencies = set(currencies.values()) - {"PLN", None}
    fx_rates = {"PLN": 1.0}
    if unique_currencies:
        fx_tickers = [f"{c}PLN=X" for c in unique_currencies]
        fx_data = yf.download(
            fx_tickers, period="1d", group_by="ticker", progress=False
        )
        for curr in unique_currencies:
            try:
                t_name = f"{curr}PLN=X"
                price = (
                    fx_data["Close"].iloc[-1]
                    if len(unique_currencies) == 1
                    else fx_data[t_name]["Close"].iloc[-1]
                )
                fx_rates[curr] = float(price)
            except:
                fx_rates[curr] = 4.0 if curr == "USD" else 4.30

    updated_portfolio["FX_Rate"] = (
        updated_portfolio["Asset_Currency"].map(fx_rates).fillna(1.0)
    )
    updated_portfolio["Current_Value_PLN"] = (
        updated_portfolio["Current_Value_Native"] * updated_portfolio["FX_Rate"]
    )
    return updated_portfolio


def calculate_cash_stats(df: pd.DataFrame) -> dict:
    deposits = df[df["Type"].isin(["Deposit", "Transfer"])]["Amount"].sum()
    div_gross = df[df["Type"] == "Dividend"]["Amount"].sum()
    wht_tax = df[df["Type"] == "Withholding tax"]["Amount"].sum()
    interest = df[df["Type"] == "Free funds interest"]["Amount"].sum()
    interest_tax = df[df["Type"] == "Free funds interest tax"]["Amount"].sum()
    return {
        "deposits": deposits,
        "dividends": div_gross + wht_tax,
        "interest": interest + interest_tax,
    }


# --- UI EXECUTION FLOW ---
try:
    # Pobieranie danych bezpośrednio z Dysku Google w tle
    raw_df = pd.read_excel(DRIVE_DOWNLOAD_URL, skiprows=4)
    raw_df.columns = raw_df.columns.str.strip()
    clean_df = raw_df.dropna(subset=["ID"])

    # Obliczenia
    base_portfolio = calculate_accurate_portfolio(clean_df)
    final_portfolio = fetch_market_and_fx_data(base_portfolio)
    cash = calculate_cash_stats(clean_df)

    # Globalne metryki
    total_value_pln = final_portfolio["Current_Value_PLN"].sum()
    total_gain_pln = (
        total_value_pln + cash["dividends"] + cash["interest"]
    ) - cash["deposits"]
    roi = (total_gain_pln / cash["deposits"]) * 100 if cash["deposits"] > 0 else 0

    # Wyświetlanie metryk
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Wycena Akcji (PLN)", f"{total_value_pln:,.2f} zł")
    col2.metric("Suma Twoich Wpłat", f"{cash['deposits']:,.2f} zł")
    col3.metric("Zysk / Strata", f"{total_gain_pln:,.2f} zł", delta=f"{roi:.2f}%")
    col4.metric(
        "Dywidendy + Odsetki", f"{(cash['dividends'] + cash['interest']):,.2f} zł"
    )

    st.markdown("---")

    # Wykresy
    chart_col1, chart_col2 = st.columns(2)
    with chart_col1:
        st.subheader("Struktura Portfela")
        fig_pie = px.pie(
            final_portfolio,
            values="Current_Value_PLN",
            names="Ticker",
            hole=0.4,
            color_discrete_sequence=px.colors.sequential.RdBu,
        )
        st.plotly_chart(fig_pie, use_container_width=True)

    with chart_col2:
        st.subheader("Wartość Pozycji w PLN")
        fig_bar = px.bar(
            final_portfolio.sort_values(by="Current_Value_PLN", ascending=True),
            x="Current_Value_PLN",
            y="Ticker",
            orientation="h",
            text_auto=",.2f",
            color="Current_Value_PLN",
            color_continuous_scale=px.colors.sequential.Viridis,
        )
        st.plotly_chart(fig_bar, use_container_width=True)

    # Tabela szczegółowa
    st.subheader("📋 Szczegóły Twoich Pozycji")
    st.dataframe(
        final_portfolio[
            [
                "Ticker",
                "Shares_Owned",
                "Asset_Currency",
                "Current_Price",
                "Current_Value_PLN",
            ]
        ],
        use_container_width=True,
    )

except Exception as e:
    st.error(
        "Błąd ładowania danych z Dysku Google. Upewnij się, że link jest publiczny (każdy może wyświetlić)."
    )
    logger.error("Drive connection failed", exc_info=True)