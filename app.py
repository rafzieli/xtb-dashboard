import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import yfinance as yf
import logging
import re
import io
import requests

# --- KONFIGURACJA ---
st.set_page_config(page_title="XTB Dashboard PLN", page_icon="💰", layout="wide")

st.title("💰 Prywatny Dashboard Finansowy XTB (Portfel PLN)")
st.markdown("---")

# --- LOGIKA OBLICZENIOWA ---
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

def fix_ticker_for_yahoo(xtb_ticker: str) -> str:
    if not isinstance(xtb_ticker, str): return xtb_ticker
    if xtb_ticker.endswith(".US"): return xtb_ticker.replace(".US", "")
    if xtb_ticker.endswith(".PL"): return xtb_ticker.replace(".PL", ".WA")
    if "." not in xtb_ticker: return f"{xtb_ticker}.WA"
    return xtb_ticker

def calculate_accurate_portfolio(df_closed: pd.DataFrame, df_cash: pd.DataFrame) -> tuple:
    realized_pnl_stocks = df_closed["Profit/Loss"].sum() if "Profit/Loss" in df_closed.columns else 0.0
    
    other_gains = df_cash[df_cash["Type"].isin([
        "Dividend", "Withholding tax", "Free funds interest", "Free funds interest tax"
    ])]["Amount"].sum()
    
    total_realized_pnl = realized_pnl_stocks + other_gains
    
    asset_types = ["Stock purchase", "Stock sell"]
    trade_df = df_cash[df_cash["Type"].isin(asset_types)].copy()
    
    if trade_df.empty:
        return pd.DataFrame(), round(total_realized_pnl, 2)
        
    parsed_data = trade_df.apply(parse_xtb_comment, axis=1)
    trade_df["Volume_Adjusted"] = [x[0] if x is not None else None for x in parsed_data]
    
    portfolio = trade_df.groupby("Ticker").agg(
        Shares_Owned=("Volume_Adjusted", "sum"),
        Net_Cash_Flow=("Amount", "sum")
    ).reset_index()
    
    portfolio["Shares_Owned"] = portfolio["Shares_Owned"].round(6)
    active_portfolio = portfolio[portfolio["Shares_Owned"] > 0.0001].copy()
    active_portfolio["Total_Invested_Raw"] = -active_portfolio["Net_Cash_Flow"]

    return active_portfolio.drop(columns=["Net_Cash_Flow"]), round(total_realized_pnl, 2)

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
            price = hist["Close"].iloc[-1] if not hist.empty else (t.info.get("previousClose") or 0.0)
            currency = t.info.get("currency", "USD")
            if price is not None and currency in ["ILA", "GBX"] and y_ticker.endswith(".WA"):
                price /= 100.0
                currency = "PLN"
            current_prices[y_ticker] = price
            currencies[y_ticker] = currency
        except Exception:
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
            fx_rates[curr] = float(hist_fx["Close"].iloc[-1]) if not hist_fx.empty else 4.00
        except:
            fx_rates[curr] = 4.00

    updated_portfolio["FX_Rate"] = updated_portfolio["Asset_Currency"].map(fx_rates).fillna(1.0)
    updated_portfolio["Current_Value_PLN"] = updated_portfolio["Current_Value_Native"] * updated_portfolio["FX_Rate"]
    return updated_portfolio

@st.cache_data(ttl=3600)
def generate_weekly_historical_timeline(df_cash: pd.DataFrame) -> pd.DataFrame:
    df_sorted = df_cash.copy()
    df_sorted["Time"] = pd.to_datetime(df_sorted["Time"])
    df_sorted = df_sorted.sort_values(by="Time")
    
    if df_sorted.empty: return pd.DataFrame()
    
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
        except Exception:
            pass

    timeline_records = []
    
    for current_day in weekly_range:
        sub_df = df_sorted[df_sorted["Time"].dt.date <= current_day]
        
        # Wkład netto na dany dzień
        dep_day = sub_df[sub_df["Type"] == "Deposit"]["Amount"].sum()
        net_deposits_day = dep_day
        
        # Wolna gotówka wyliczana jako czysta suma Amount (bez wiersza Total, bo został odfiltrowany na starcie)
        cash_balance = sub_df["Amount"].sum()
        
        stock_value_pln = 0.0
        if not trade_df.empty:
            sub_trades = trade_df[trade_df["Time"].dt.date <= current_day]
            if not sub_trades.empty:
                shares_per_ticker = sub_trades.groupby("Yahoo_Ticker")["Volume_Adjusted"].sum()
                for t_symbol, shares in shares_per_ticker.items():
                    if shares > 0.0001:
                        price = None
                        if t_symbol in weekly_prices:
                            available_prices = weekly_prices[t_symbol].loc[weekly_prices[t_symbol].index.date <= current_day]
                            if not available_prices.empty:
                                price = available_prices.iloc[-1]
                        
                        if pd.isna(price) or price is None:
                            price = 0.0 
                        
                        fx_rate = 1.0
                        if t_symbol.endswith(".US") or ("." not in t_symbol and not t_symbol.endswith(".WA")): 
                            fx_rate = 4.00
                        elif not t_symbol.endswith(".WA"): 
                            fx_rate = 4.30
                        
                        stock_value_pln += shares * price * fx_rate
                        
        total_portfolio_value = cash_balance + stock_value_pln
        wynik = total_portfolio_value - net_deposits_day
        
        timeline_records.append({
            "Time": current_day,
            "Wpłaty": net_deposits_day,
            "Wartość": total_portfolio_value,
            "Wynik": wynik
        })
        
    return pd.DataFrame(timeline_records)

# --- UI EXECUTION FLOW ---
try:
    GOOGLE_DRIVE_FILE_ID = "1icRPA0GdmAXU-U-WF_65QD1RxAfSc8oH"
    DRIVE_DOWNLOAD_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_DRIVE_FILE_ID}/export?format=xlsx"

    with st.spinner("Przetwarzanie danych i konfiguracja Dark Mode..."):
        response = requests.get(DRIVE_DOWNLOAD_URL, timeout=10)

    if response.status_code == 200:
        df_closed = pd.read_excel(io.BytesIO(response.content), sheet_name="Closed Positions", skiprows=4)
        df_cash = pd.read_excel(io.BytesIO(response.content), sheet_name="Cash Operations", skiprows=4)
        
        df_closed.columns = df_closed.columns.str.strip()
        df_cash.columns = df_cash.columns.str.strip()

        # 🔥 POPRAWKA: Dynamicznie odrzucamy wiersze podsumowujące "Total" generowane przez XTB na końcu tabeli
        if not df_closed.empty:
            df_closed = df_closed[df_closed.iloc[:, 0].astype(str).str.strip().str.upper() != 'TOTAL']
        if not df_cash.empty:
            df_cash = df_cash[df_cash.iloc[:, 0].astype(str).str.strip().str.upper() != 'TOTAL']

        # Obliczenia bazy
        base_portfolio, realized_pnl = calculate_accurate_portfolio(df_closed, df_cash)
        final_portfolio = fetch_market_and_fx_data(base_portfolio)
        timeline_df = generate_weekly_historical_timeline(df_cash)

        # 1. Wycena aktualnych akcji
        total_value_stocks = final_portfolio["Current_Value_PLN"].sum() if not final_portfolio.empty else 0.0
        
        # 2. Czyste wyliczenie wolnych środków (Suma całej oczyszczonej kolumny Amount)
        current_free_cash = df_cash["Amount"].sum()
        
        # 3. Wartość całkowita portfela (Akcje + Wolna gotówka)
        total_portfolio_value = total_value_stocks + current_free_cash
        
        # 4. Wkład Netto (tylko rzeczywiste depozyty)
        total_invested = df_cash[df_cash["Type"] == "Deposit"]["Amount"].sum()
        
        # 5. Wynik Portfela (Różnica wartości rynkowej i wpłat)
        total_gain_pln = total_portfolio_value - total_invested
        roi = (total_gain_pln / total_invested) * 100 if total_invested > 0 else 0

        # --- PANEL METRYK ---
        st.subheader("📊 Podsumowanie Twojego Portfela")
        
        col1, col2, col3 = st.columns(3)
        col1.metric("Wycena Akcji", f"{total_value_stocks:,.2f} zł")
        col2.metric("Wolne Środki (PLN)", f"{current_free_cash:,.2f} zł")
        col3.metric("Wartość Całkowita Portfela", f"{total_portfolio_value:,.2f} zł")
        
        st.write("") 
        
        col4, col5, col6 = st.columns(3)
        col4.metric("Suma Wpłat (Wkład Netto)", f"{total_invested:,.2f} zł")
        col5.metric("Wynik Portfela (Zysk/Strata)", f"{total_gain_pln:,.2f} zł", delta=f"{roi:.2f}%")
        col6.metric("Zrealizowany Zysk 🟢 (Historia)", f"{realized_pnl:,.2f} zł")

        st.markdown("---")

        # --- WYKRES LINIOWY (DARK MODE + HOVER Z WYNIKIEM) ---
        st.subheader("📈 Zmiana Wartości Portfela w Czasie")
        
        line_color = '#00E676' if total_gain_pln >= 0 else '#FF1744'

        fig_line = go.Figure()
        
        # Linia wkładu
        fig_line.add_trace(go.Scatter(
            x=timeline_df["Time"], y=timeline_df["Wpłaty"],
            mode='lines', name='Suma Wpłat',
            line=dict(color='#78909C', width=2, dash='dash'),
            hovertemplate="Data: %{x}<br>Suma Wpłat: %{y:,.2f} zł<extra></extra>"
        ))
        
        # Linia wartości portfela z jawnym WYNIKIEM na hoverze
        fig_line.add_trace(go.Scatter(
            x=timeline_df["Time"], y=timeline_df["Wartość"],
            mode='lines', name='Wartość Całkowita',
            customdata=timeline_df["Wynik"],
            line=dict(color=line_color, width=3),
            hovertemplate="Data: %{x}<br>Wartość Portfela: %{y:,.2f} zł<br><b>Wynik (Różnica): %{customdata:,.2f} zł</b><extra></extra>"
        ))
        
        fig_line.update_layout(
            template="plotly_dark",
            hovermode="x unified",
            xaxis_title="",
            yaxis_title="Wartość (PLN)",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=30, b=0)
        )
        st.plotly_chart(fig_line, use_container_width=True)

        st.markdown("---")

        # --- WYKRESY STRUKTURY (DARK MODE) ---
        chart_col1, chart_col2 = st.columns(2)
        with chart_col1:
            st.subheader("Struktura Portfela (Akcje)")
            if not final_portfolio.empty:
                fig_pie = px.pie(final_portfolio, values="Current_Value_PLN", names="Ticker", hole=0.4,
                                 color_discrete_sequence=px.colors.cyclical.IceFire)
                fig_pie.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)")
                st.plotly_chart(fig_pie, use_container_width=True)

        with chart_col2:
            st.subheader("Wartość Pozycji (PLN)")
            if not final_portfolio.empty:
                fig_bar = px.bar(
                    final_portfolio.sort_values(by="Current_Value_PLN", ascending=True), 
                    x="Current_Value_PLN", y="Ticker", orientation="h",
                    text_auto=",.2f", color="Current_Value_PLN",
                    color_continuous_scale="blues"
                )
                fig_bar.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)")
                st.plotly_chart(fig_bar, use_container_width=True)

        # --- TABELA ---
        st.subheader("📋 Szczegóły Twoich Pozycji")
        if not final_portfolio.empty:
            st.dataframe(final_portfolio[["Ticker", "Shares_Owned", "Asset_Currency", "Current_Price", "Current_Value_PLN"]], use_container_width=True)

    else:
        st.error(f"Błąd pobierania raportu. Status: {response.status_code}")

except Exception as e:
    st.error("💥 Wystąpił błąd podczas generowania dashboardu.")
    st.exception(e)