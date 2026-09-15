# 📈Scalper — Multi-User Prediction-Based Options Trading Platform

An automated **options scalping platform** that generates BUY CE / BUY PE signals from astrological (astro-logic) forecast data, executes trades on live market feeds, and supports **multiple paying users** through a secure login system and an integrated payment gateway.

---

## ✨ Features

- 🔐 Multi-User Authentication — login/password-based access with isolated sessions per user
- 💳 Integrated Payment Gateway — scalper access unlocks only after successful payment
- 🌠 Astro-Based Signal Engine — converts weekly astro forecast reports into actionable trade signals
- 📈 Automated CE/PE Trade Execution — places Buy Call or Buy Put trades based on prediction signals
- 🧮 Slicing/Grid-Based Trade Management — dynamic, price-level based entry and exit logic
- ⏱️ Trading-Hours Enforcement — trades restricted to a defined market window
- 🔄 CE/PE Alternation Control — enforces alternating trade direction to manage exposure
- 📊 Live Dashboard — real-time P&L, active positions, and signal feed per user
- 🧪 Paper & Live Mode Toggle — test strategies risk-free before going live with real capital

---

## 🔮 How the Astro Signal Logic Works

1. 📥 A weekly astro forecast report (CSV) is uploaded, containing `Date`, `Time`, and `U/D Logic` (Upside / Downside / Neutral) per timeframe
2. 🧹 The system parses timestamps and normalizes the report into a clean signal table
3. 🔍 A rolling **3-row window** is evaluated:
   - ✅ 3 consecutive **Upside** rows → generate a **BUY CE** signal
   - ✅ 3 consecutive **Downside** rows → generate a **BUY PE** signal
   - ⛔ Mixed rows → **no trade**
4. 🎯 Signals pass through entry filters — one active trade at a time, fixed trading window, and mandatory CE/PE alternation between consecutive trades

---

## ⚙️ Trade Execution Flow

1. 🔑 User logs in and completes payment via the integrated payment gateway
2. 📡 System fetches live LTP and resolves the nearest tradeable strike
3. 🌠 Astro signal engine evaluates the current forecast window
4. 📈📉 A BUY CE or BUY PE order is placed based on the signal direction
5. 🧮 Position is managed using slicing/grid-based profit and loss triggers
6. 🔁 System resets and prepares for the next eligible signal, honoring the CE/PE alternation rule

---

## 🛠️ Tech Stack

- 🐍 Python — core backend & signal logic
- 📡 Broker API Integration — live order placement & market data
- 💳 Payment Gateway — subscription/access control
- 🔐 Auth System — per-user login & session management
- 📊 Dashboard — live positions, P&L, and signal feed visualization

---

## 🚀 Installation

```bash
git clone https://github.com/your-username/astro-scalper.git
cd astro-scalper
pip install -r requirements.txt
python main.py
```

---

## 👨‍💻 Author

Developed by **Krish Sabharwal** 🚀
