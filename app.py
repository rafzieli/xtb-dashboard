import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
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

st.title("💰 :rat: Szczur Dashboard :rat: 💰")
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
    
    # Czyszczenie i ujednolicenie tickerów, żeby grupy były idealne
    trade_df["Ticker"] = trade_df["Ticker"].str.strip().str.upper()
    
    parsed_data = trade_df.apply(parse_xtb_comment, axis=1)
    trade_df["Volume_Adjusted"] = [x[0] if x is not None else None for x in parsed_data]
    trade_df["Price"] = [x[1] if x is not None else None for x in parsed_data]
    
    realized_pnl = 0.0
    
    # Precyzyjne rozliczenie FIFO dla każdego tickera z osobna
    for ticker, group in trade_df.groupby("Ticker"):
        buy_queue = []
        group_sorted = group.sort_values(by="Time")
        
        for idx, row in group_sorted.iterrows():
            vol = row["Volume_Adjusted"]
            amount = row["Amount"]
            
            if row["Type"] == "Stock purchase" and vol is not None:
                # Zapisujemy ilość i faktycznie wydaną kwotę PLN (jako wartość dodatnią do kosztu)
                buy_queue.append({"vol": vol, "total_cost": abs(amount)})
                
            elif row["Type"] == "Stock sell" and vol is not None:
                sell_vol = abs(vol)
                revenue = amount # Przychód ze sprzedaży (dodatni)
                matched_cost = 0.0
                
                # Pobieramy koszt z kolejki zakupów
                while sell_vol > 0.000001 and len(buy_queue) > 0:
                    current_buy = buy_queue[0]
                    if current_buy["vol"] <= sell_vol + 0.000001:
                        # Wykorzystujemy cały ten zakup
                        matched_cost += current_buy["total_cost"]
                        sell_vol -= current_buy["vol"]
                        buy_queue.pop(0)
                    else:
                        # Wykorzystujemy część tego zakupu
                        proporcja = sell_vol / current_buy["vol"]
                        matched_cost += current_buy["total_cost"] * proporcja
                        current_buy["total_cost"] -= current_buy["total_cost"] * proporcja
                        current_buy["vol"] -= sell_vol
                        sell_vol = 0
                
                # Zysk = Przychód - Koszt zakupu danej transakcji
                realized_pnl += (revenue - matched_cost)

    # Budujemy aktywny portfel na sam koniec
    portfolio = trade_df.groupby("Ticker").agg(
        Shares_Owned=("Volume_Adjusted", "sum"),
        Net_Cash_Flow=("Amount", "sum")
    ).reset_index()
    
    # Jeśli zostają mikro-ochłapy przez zaokrąglenia XTB (np. mniej niż 0.0001 akcji), czyścimy do zera
    portfolio["Shares_Owned"] = portfolio["Shares_Owned"].round(6)
    active_portfolio = portfolio[portfolio["Shares_Owned"] > 0.0001].copy()
    active_portfolio["Total_Invested_Raw"] = -active_portfolio["Net_Cash_Flow"]

    return active_portfolio.drop(columns=["Net_Cash_Flow"]), round(realized_pnl, 2)

    # Wyliczenie obecnego stanu posiadania do tabeli i struktury
    portfolio = trade_df.groupby("Ticker").agg(
        Shares_Owned=("Volume_Adjusted", "sum"),
        Net_Cash_Flow=("Amount", "sum")
    ).reset_index()
    portfolio["Shares_Owned"] = portfolio["Shares_Owned"].round(6)
    active_portfolio = portfolio[portfolio["Shares_Owned"] > 0].copy()
    active_portfolio["Total_Invested_Raw"] = -active_portfolio["Net_Cash_Flow"]

    return active_portfolio.drop(columns=["Net_Cash_Flow"]), round(realized_pnl, 2)

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
    
    current_prices, currencies = {}, {}
    tickers_to_fetch = updated_portfolio["Yahoo_Ticker"].unique()
    
    for y_ticker in tickers_to_fetch:
        try:
            t = yf.Ticker(y_ticker)
            hist = t.history(period="1d", timeout=3)
            if not hist.empty:
                price = hist["Close"].iloc[-1]
            else:
                price = t.info.get("previousClose") or t.info.get("regularMarketPrice")
            currency = t.info.get("currency", "USD")
            if price is not None and currency in ["ILA", "GBX"] and y_ticker.endswith(".WA"):
                price /= 100.0
                currency = "PLN"
            current_prices[y_ticker] = price
            currencies[y_ticker] = currency
        except Exception as e:
            logger.error(f"Error fetching {y_ticker}: {e}")
            current_prices[y_ticker], currencies[y_ticker] = None, "USD"

    updated_portfolio["Current_Price"] = updated_portfolio["Yahoo_Ticker"].map(current_prices)
    updated_portfolio["Asset_Currency"] = updated_portfolio["Yahoo_Ticker"].map(currencies).fillna("USD")
    updated_portfolio["Current_Value_Native"] = updated_portfolio["Shares_Owned"] * updated_portfolio["Current_Price"]
    
    unique_currencies = set(updated_portfolio["Asset_Currency"].dropna().unique()) - {"PLN"}
    fx_rates = {"PLN": 1.0}
    
    for curr in unique_currencies:
        try:
            fx_ticker = f"{curr}PLN=X"
            t_fx = yf.Ticker(fx_ticker)
            hist_fx = t_fx.history(period="1d", timeout=3)
            if not hist_fx.empty:
                fx_rates[curr] = float(hist_fx["Close"].iloc[-1])
            else:
                fx_rates[curr] = 4.00 if curr == "USD" else 4.30
        except:
            fx_rates[curr] = 4.00 if curr == "USD" else 4.30

    updated_portfolio["FX_Rate"] = updated_portfolio["Asset_Currency"].map(fx_rates).fillna(1.0)
    updated_portfolio["Current_Value_PLN"] = updated_portfolio["Current_Value_Native"] * updated_portfolio["FX_Rate"]
    return updated_portfolio

def calculate_cash_stats(df: pd.DataFrame) -> dict:
    # Wpłaty Deposit pomniejszone o Transfery PLN to USD
    deposits_only = df[df["Type"] == "Deposit"]["Amount"].sum()
    to_usd_transfers = df[(df["Type"] == "Transfer") & (df["Comment"].str.contains("PLN to USD", na=False, case=False))]["Amount"].sum()
    
    real_pln_deposits = deposits_only + to_usd_transfers
    
    div_gross = df[df["Type"] == "Dividend"]["Amount"].sum()
    wht_tax = df[df["Type"] == "Withholding tax"]["Amount"].sum()
    interest = df[df["Type"] == "Free funds interest"]["Amount"].sum()
    interest_tax = df[df["Type"] == "Free funds interest tax"]["Amount"].sum()
    return {
        "deposits": real_pln_deposits,
        "dividends": div_gross + wht_tax,
        "interest": interest + interest_tax
    }

@st.cache_data(ttl=3600)
def generate_weekly_historical_timeline(df: pd.DataFrame) -> pd.DataFrame:
    """Generates a weekly timeline including ALL stocks (.US too) but tracking PLN net deposits."""
    df_sorted = df.copy()
    df_sorted["Time"] = pd.to_datetime(df_sorted["Time"])
    df_sorted = df_sorted.sort_values(by="Time")
    
    start_date = df_sorted["Time"].min().date()
    end_date = df_sorted["Time"].max().date()
    
    weekly_range = pd.date_range(start=start_date, end=end_date, freq="W-FRI").date
    if len(weekly_range) == 0 or weekly_range[-1] < end_date:
        weekly_range = list(weekly_range) + [end_date]

    asset_types = ["Stock purchase", "Stock sell"]
    trade_df = df_sorted[df_sorted["Type"].isin(asset_types)].copy()
    
    if not trade_df.empty:
        parsed_data = trade_df.apply(parse_xtb_comment, axis=1)
        trade_df["Volume_Adjusted"] = [x[0] if x is not None else None for x in parsed_data]
        trade_df["Price"] = [x[1] if x is not None else None for x in parsed_data]
        trade_df["Yahoo_Ticker"] = trade_df["Ticker"].apply(fix_ticker_for_yahoo)
    
    unique_tickers = trade_df["Yahoo_Ticker"].dropna().unique() if not trade_df.empty else []
    
    weekly_prices = {}
    if len(unique_tickers) > 0:
        try:
            hist_data = yf.download(list(unique_tickers), period="max", interval="1wk", group_by='ticker', progress=False)
            for ticker in unique_tickers:
                if len(unique_tickers) == 1:
                    weekly_prices[ticker] = hist_data["Close"]
                else:
                    if ticker in hist_data.columns.levels[0]:
                        weekly_prices[ticker] = hist_data[ticker]["Close"]
        except Exception as e:
            logger.error(f"Error downloading weekly historical data: {e}")

    timeline_records = []
    
    for current_day in weekly_range:
        sub_df = df_sorted[df_sorted["Time"].dt.date <= current_day]
        
        # Wkład netto na dany tydzień (Wpłaty minus transfery PLN to USD)
        dep_day = sub_df[sub_df["Type"] == "Deposit"]["Amount"].sum()
        trans_day = sub_df[(sub_df["Type"] == "Transfer") & (sub_df["Comment"].str.contains("PLN to USD", na=False, case=False))]["Amount"].sum()
        net_deposits_day = dep_day + trans_day
        
        # Saldo gotówki w XTB
        cash_balance = sub_df["Amount"].sum()
        
        stock_value_pln = 0.0
        if not trade_df.empty:
            sub_trades = trade_df[trade_df["Time"].dt.date <= current_day]
            if not sub_trades.empty:
                shares_per_ticker = sub_trades.groupby("Yahoo_Ticker")["Volume_Adjusted"].sum()
                
                for t_symbol, shares in shares_per_ticker.items():
                    if shares > 0.00001:
                        price = None
                        if t_symbol in weekly_prices:
                            available_prices = weekly_prices[t_symbol].loc[weekly_prices[t_symbol].index.date <= current_day]
                            if not available_prices.empty:
                                price = available_prices.iloc[-1]
                        
                        if pd.isna(price) or price is None:
                            price = sub_trades[sub_trades["Yahoo_Ticker"] == t_symbol]["Price"].iloc[-1]
                        
                        # Przelicznik walutowy dla wyceny historycznej akcji
                        fx_rate = 1.0
                        if t_symbol.endswith(".US") or ("." not in t_symbol and not t_symbol.endswith(".WA")): 
                            fx_rate = 4.00
                        elif not t_symbol.endswith(".WA"): 
                            fx_rate = 4.30
                        
                        stock_value_pln += shares * price * fx_rate
                        
        total_portfolio_value = cash_balance + stock_value_pln
        
        timeline_records.append({
            "Time": current_day,
            "Wpłaty Rzeczywiste (Wkład Netto)": net_deposits_day,
            "Realna Wartość Portfela": total_portfolio_value
        })
        
    return pd.DataFrame(timeline_records)

# --- UI EXECUTION FLOW ---
try:
    GOOGLE_DRIVE_FILE_ID = "1icRPA0GdmAXU-U-WF_65QD1RxAfSc8oH"
    DRIVE_DOWNLOAD_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_DRIVE_FILE_ID}/export?format=xlsx"

    with st.spinner("Przetwarzanie pełnego portfela (PLN + US)..."):
        response = requests.get(DRIVE_DOWNLOAD_URL, timeout=10)

    if response.status_code == 200:
        raw_df = pd.read_excel(io.BytesIO(response.content), skiprows=4, skipfooter=1)
        raw_df.columns = raw_df.columns.str.strip()
        clean_df = raw_df.dropna(subset=["ID"])

        # Obliczenia bazowe
        base_portfolio, realized_pnl = calculate_accurate_portfolio(clean_df)
        final_portfolio = fetch_market_and_fx_data(base_portfolio)
        cash = calculate_cash_stats(clean_df)
        timeline_df = generate_weekly_historical_timeline(clean_df)
        total_free_cash = clean_df['Amount'].sum()

        total_value_stocks = final_portfolio["Current_Value_PLN"].sum() if not final_portfolio.empty else 0.0
        total_gain_pln = (total_value_stocks + cash["interest"]) - cash["deposits"]
        roi = (total_gain_pln / cash["deposits"]) * 100 if cash["deposits"] > 0 else 0

        # --- PANEL METRYK ---
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Wycena Akcji (PLN)", f"{total_value_stocks:,.2f} zł")
        col2.metric("Suma Twoich Wpłat Netto", f"{cash['deposits']:,.2f} zł")
        col3.metric("Zrealizowany Zysk 🟢", f"{realized_pnl:,.2f} zł")
        col4.metric("Dywidendy + Odsetki", f"{(cash['dividends'] + cash['interest']):,.2f} zł")
        col5.metric("Łączny Wynik Portfela", f"{total_gain_pln:,.2f} zł", delta=f"{roi:.2f}%")

        st.write("")
        
        col1, col2 = st.columns(2)
        col1.metric("Wycena Portfela (PLN)", f"{total_value_stocks + total_free_cash:,.2f} zł")
        col2.metric("Wolne srodki (PLN)", f"{total_free_cash:,.2f} zł")

        st.markdown("---")

        # --- WYKRES LINIOWY (TYGODNIOWY) ---
        st.subheader("📈 Realna zmiana wartości portfela w czasie vs Twoje wpłaty")
        
        fig_line = go.Figure()
        fig_line.add_trace(go.Scatter(
            x=timeline_df["Time"], y=timeline_df["Wpłaty Rzeczywiste (Wkład Netto)"],
            mode='lines', name='Twój Realny Wkład (Wpłaty Netto)',
            line=dict(color='rgba(150, 150, 150, 0.8)', width=2, dash='dash')
        ))
        fig_line.add_trace(go.Scatter(
            x=timeline_df["Time"], y=timeline_df["Realna Wartość Portfela"],
            mode='lines', name='Rynkowa Wartość (Akcje + Gotówka)',
            line=dict(color='#cc0000' if total_gain_pln < 0 else '#2ca02c', width=3)
        ))
        
        fig_line.update_layout(
            hovermode="x unified",
            xaxis_title="Data (Zamknięcia Tygodniowe)",
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
    st.error("💥 Wystąpił błąd podczas generowania kompletnego dashboardu.")
    st.exception(e)