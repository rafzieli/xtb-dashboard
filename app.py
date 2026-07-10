import streamlit as st
import pandas as pd
import plotly.express as px
import yfinance as yf
import logging
import re
import io
import requests

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- SET CONFIGURATION ---
st.set_page_config(page_title="XTB Dashboard", page_icon="💰", layout="wide")

st.title("💰 Prywatny Dashboard Finansowy XTB (Debug Mode)")
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

def calculate_accurate_portfolio(df: pd.DataFrame) -> pd.DataFrame:
    logger.info("Rozpoczęcie kalkulacji portfolio z danych transakcyjnych...")
    asset_types = ["Stock purchase", "Stock sell"]
    trade_df = df[df["Type"].isin(asset_types)].copy()
    if trade_df.empty: 
        logger.warning("Brak transakcji kupna/sprzedaży akcji!")
        return pd.DataFrame()
    parsed_data = trade_df.apply(parse_xtb_comment, axis=1)
    trade_df["Volume_Adjusted"] = [x[0] if x is not None else None for x in parsed_data]
    trade_df["Price"] = [x[1] if x is not None else None for x in parsed_data]
    portfolio = trade_df.groupby("Ticker").agg(
        Shares_Owned=("Volume_Adjusted", "sum"),
        Net_Cash_Flow=("Amount", "sum")
    ).reset_index()
    portfolio["Shares_Owned"] = portfolio["Shares_Owned"].round(6)
    active_portfolio = portfolio[portfolio["Shares_Owned"] > 0].copy()
    active_portfolio["Total_Invested_Raw"] = -active_portfolio["Net_Cash_Flow"]
    logger.info(f"Kalkulacja zakończona. Znaleziono {len(active_portfolio)} aktywnych pozycji.")
    return active_portfolio.drop(columns=["Net_Cash_Flow"])

def fix_ticker_for_yahoo(xtb_ticker: str) -> str:
    if not isinstance(xtb_ticker, str): return xtb_ticker
    if xtb_ticker.endswith(".US"): return xtb_ticker.replace(".US", "")
    if xtb_ticker.endswith(".PL"): return xtb_ticker.replace(".PL", ".WA")
    if "." not in xtb_ticker: return f"{xtb_ticker}.WA"
    return xtb_ticker

@st.cache_data(ttl=600)
def fetch_market_and_fx_data(portfolio_df: pd.DataFrame):
    if portfolio_df.empty: return portfolio_df
    updated_portfolio = portfolio_df.copy()
    updated_portfolio["Yahoo_Ticker"] = updated_portfolio["Ticker"].apply(fix_ticker_for_yahoo)
    
    current_prices = {}
    currencies = {}
    
    tickers_to_fetch = updated_portfolio["Yahoo_Ticker"].unique()
    logger.info(f"Rozpoczęcie pobierania cen dla tickerów: {tickers_to_fetch}")
    
    # Bezpieczne pobieranie cen po kolei z logowaniem każdego kroku
    for y_ticker in tickers_to_fetch:
        logger.info(f"-> Odpytywanie Yahoo Finance o ticker: {y_ticker}")
        try:
            t = yf.Ticker(y_ticker)
            hist = t.history(period="1d", timeout=3) # Krótki timeout 3 sekundy
            if not hist.empty:
                price = hist["Close"].iloc[-1]
                logger.info(f"   [SUKCES] Cena historyczna dla {y_ticker}: {price}")
            else:
                price = t.info.get("previousClose") or t.info.get("regularMarketPrice")
                logger.info(f"   [INFO] Brak historii, używam ceny info dla {y_ticker}: {price}")
                
            currency = t.info.get("currency", "USD")
            
            if price is not None and currency in ["ILA", "GBX"] and y_ticker.endswith(".WA"):
                price /= 100.0
                currency = "PLN"
                logger.info(f"   [GPW FIX] Przeliczono grosze dla {y_ticker}: {price} PLN")
                
            current_prices[y_ticker] = price
            currencies[y_ticker] = currency
        except Exception as e:
            logger.error(f"   [BŁĄD] Nie udało się pobrać danych dla {y_ticker}: {e}")
            current_prices[y_ticker] = None
            currencies[y_ticker] = "USD"

    updated_portfolio["Current_Price"] = updated_portfolio["Yahoo_Ticker"].map(current_prices)
    updated_portfolio["Asset_Currency"] = updated_portfolio["Yahoo_Ticker"].map(currencies).fillna("USD")
    updated_portfolio["Current_Value_Native"] = updated_portfolio["Shares_Owned"] * updated_portfolio["Current_Price"]
    
    # Pobieranie kursów walut
    unique_currencies = set(updated_portfolio["Asset_Currency"].dropna().unique()) - {"PLN"}
    fx_rates = {"PLN": 1.0}
    
    logger.info(f"Rozpoczęcie pobierania kursów wymiany dla walut: {unique_currencies}")
    for curr in unique_currencies:
        try:
            fx_ticker = f"{curr}PLN=X"
            logger.info(f"-> Odpytywanie o kurs walutowy: {fx_ticker}")
            t_fx = yf.Ticker(fx_ticker)
            hist_fx = t_fx.history(period="1d", timeout=3)
            if not hist_fx.empty:
                fx_rates[curr] = float(hist_fx["Close"].iloc[-1])
                logger.info(f"   [SUKCES] Kurs {fx_ticker}: {fx_rates[curr]}")
            else:
                fx_rates[curr] = 4.00 if curr == "USD" else 4.30
                logger.info(f"   [FALLBACK] Używam sztywnego kursu dla {curr}: {fx_rates[curr]}")
        except Exception as e:
            logger.error(f"   [BŁĄD] Kurs waluty {curr} nieudany: {e}")
            fx_rates[curr] = 4.00 if curr == "USD" else 4.30

    updated_portfolio["FX_Rate"] = updated_portfolio["Asset_Currency"].map(fx_rates).fillna(1.0)
    updated_portfolio["Current_Value_PLN"] = updated_portfolio["Current_Value_Native"] * updated_portfolio["FX_Rate"]
    logger.info("Zakończono pełne pobieranie danych rynkowych.")
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
        "interest": interest + interest_tax
    }

# --- UI EXECUTION FLOW WITH VISUAL SPINNERS ---
try:
    GOOGLE_DRIVE_FILE_ID = "1icRPA0GdmAXU-U-WF_65QD1RxAfSc8oH"
    DRIVE_DOWNLOAD_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_DRIVE_FILE_ID}/export?format=xlsx"

    # Krok 1: Dysk Google
    with st.spinner("Krok 1/4: Pobieranie pliku raportu z Dysku Google..."):
        logger.info("Wysyłanie zapytania do Google Drive...")
        response = requests.get(DRIVE_DOWNLOAD_URL, timeout=10)
        logger.info(f"Odpowiedź Google Drive odebrana. Kod statusu HTTP: {response.status_code}")

    if response.status_code == 200:
        # Krok 2: Parsowanie Excela
        with st.spinner("Krok 2/4: Wczytywanie i parsowanie pliku Excel (XTB)..."):
            logger.info("Uruchamianie pd.read_excel...")
            raw_df = pd.read_excel(io.BytesIO(response.content), skiprows=4)
            raw_df.columns = raw_df.columns.str.strip()
            clean_df = raw_df.dropna(subset=["ID"])
            
            logger.info("Wywoływanie kalkulatora pozycji...")
            base_portfolio = calculate_accurate_portfolio(clean_df)

        # Krok 3: Yahoo Finance
        with st.spinner("Krok 3/4: Pobieranie cen live z Yahoo Finance (to może chwilę potrwać)..."):
            final_portfolio = fetch_market_and_fx_data(base_portfolio)
            cash = calculate_cash_stats(clean_df)

        # Krok 4: Generowanie interfejsu
        with st.spinner("Krok 4/4: Renderowanie wykresów i tabel..."):
            total_value_pln = final_portfolio["Current_Value_PLN"].sum()
            total_gain_pln = (total_value_pln + cash["dividends"] + cash["interest"]) - cash["deposits"]
            roi = (total_gain_pln / cash["deposits"]) * 100 if cash["deposits"] > 0 else 0

            # --- METRYKI ---
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Wycena Akcji (PLN)", f"{total_value_pln:,.2f} zł")
            col2.metric("Suma Twoich Wpłat", f"{cash['deposits']:,.2f} zł")
            col3.metric("Zysk / Strata", f"{total_gain_pln:,.2f} zł", delta=f"{roi:.2f}%")
            col4.metric("Dywidendy + Odsetki", f"{(cash['dividends'] + cash['interest']):,.2f} zł")

            st.markdown("---")

            # --- WYKRESY ---
            chart_col1, chart_col2 = st.columns(2)
            with chart_col1:
                st.subheader("Struktura Portfela")
                fig_pie = px.pie(final_portfolio, values="Current_Value_PLN", names="Ticker", hole=0.4,
                                 color_discrete_sequence=px.colors.sequential.RdBu)
                st.plotly_chart(fig_pie, use_container_width=True)

            with chart_col2:
                st.subheader("Wartość Pozycji w PLN")
                fig_bar = px.bar(final_portfolio.sort_values(by="Current_Value_PLN", ascending=True), 
                                 x="Current_Value_PLN", y="Ticker", orientation="h",
                                 text_auto=",.2f", color="Current_Value_PLN",
                                 color_continuous_scale=px.colors.sequential.Viridis)
                st.plotly_chart(fig_bar, use_container_width=True)

            # --- TABELA ---
            st.subheader("📋 Szczegóły Twoich Pozycji")
            st.dataframe(final_portfolio[["Ticker", "Shares_Owned", "Asset_Currency", "Current_Price", "Current_Value_PLN"]], use_container_width=True)

    else:
        st.error(f"❌ Dysk Google odrzucił połączenie. Status HTTP: {response.status_code}. Upewnij się, że plik jest udostępniony publicznie.")

except Exception as e:
    st.error("💥 Wystąpił krytyczny błąd aplikacji podczas wykonywania kodu.")
    st.exception(e) # To wyrzuci pełny traceback błędu bezpośrednio na ekranie strony www!
    logger.error("Krytyczny błąd pętli głównej", exc_info=True)