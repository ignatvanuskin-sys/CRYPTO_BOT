import { useEffect, useState } from "react"

const API = import.meta.env.VITE_API_URL || ""

function useInitData() {
  // @ts-ignore
  return window.Telegram?.WebApp?.initData || ""
}

export default function App() {
  const [tab, setTab] = useState<"trade"|"positions"|"profile">("trade")
  const [symbol, setSymbol] = useState("BTCUSDT")
  const [side, setSide] = useState<"LONG"|"SHORT">("LONG")
  const [qty, setQty] = useState("0.01")
  const [tp, setTp] = useState("")
  const [sl, setSl] = useState("")
  const [balance, setBalance] = useState<any>(null)
  const initData = useInitData()

  useEffect(() => {
    fetch(`${API}/api/account`, { headers: { "X-Telegram-Init-Data": initData } })
      .then(r => r.json()).then(setBalance).catch(()=>{})
  }, [initData])

  const openPos = async () => {
    const res = await fetch(`${API}/api/positions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": initData, "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ symbol, side, quantity: parseFloat(qty), takeProfit: tp?parseFloat(tp):null, stopLoss: sl?parseFloat(sl):null })
    })
    const data = await res.json()
    alert(res.ok ? `Opened ${data.id}` : data.detail?.message || JSON.stringify(data))
  }

  return (
    <div style={{fontFamily:"system-ui", padding:16, maxWidth:480, margin:"0 auto"}}>
      <div style={{background:"#1a1a2e", color:"white", padding:12, borderRadius:12, marginBottom:16}}>
        <div>PAPER TRADING</div>
        <div>Balance: {balance?.cash_balance || "..."} USD</div>
        <div>Equity: {balance?.equity || "..."} | PnL: {balance?.total_pnl || "..."}</div>
      </div>
      <div style={{display:"flex", gap:8, marginBottom:16}}>
        <button onClick={()=>setTab("trade")} style={{flex:1, background:tab==="trade"?"#0f3460":"#eee", color:tab==="trade"?"white":"black", padding:12, borderRadius:8}}>📈 Trade</button>
        <button onClick={()=>setTab("positions")} style={{flex:1, background:tab==="positions"?"#0f3460":"#eee", color:tab==="positions"?"white":"black", padding:12, borderRadius:8}}>📋 Positions</button>
        <button onClick={()=>setTab("profile")} style={{flex:1, background:tab==="profile"?"#0f3460":"#eee", color:tab==="profile"?"white":"black", padding:12, borderRadius:8}}>👤 Profile</button>
      </div>
      {tab==="trade" && (
        <div>
          <div style={{display:"flex", gap:8, marginBottom:12}}>
            {["BTCUSDT","ETHUSDT","SOLUSDT"].map(s => (
              <button key={s} onClick={()=>setSymbol(s)} style={{padding:"8px 12px", borderRadius:8, background:symbol===s?"#e94560":"#eee"}}>{s.replace("USDT","")}</button>
            ))}
          </div>
          <div style={{height:200, background:"#f0f0f0", display:"flex", alignItems:"center", justifyContent:"center", borderRadius:12, marginBottom:12}}>[ CHART {symbol} ]</div>
          <div style={{marginBottom:8}}>Balance $10,000 | Available {balance?.available_margin || "..."}</div>
          <input value={qty} onChange={e=>setQty(e.target.value)} placeholder="Position size" style={{width:"100%", padding:12, marginBottom:8, borderRadius:8, border:"1px solid #ccc"}} />
          <div style={{display:"flex", gap:8, marginBottom:8}}>
            <button onClick={()=>setSide("LONG")} style={{flex:1, padding:12, background:side==="LONG"?"#2ecc71":"#eee", borderRadius:8}}>LONG</button>
            <button onClick={()=>setSide("SHORT")} style={{flex:1, padding:12, background:side==="SHORT"?"#e74c3c":"#eee", borderRadius:8}}>SHORT</button>
          </div>
          <input value={tp} onChange={e=>setTp(e.target.value)} placeholder="Take Profit (optional)" style={{width:"100%", padding:12, marginBottom:8, borderRadius:8, border:"1px solid #ccc"}} />
          <input value={sl} onChange={e=>setSl(e.target.value)} placeholder="Stop Loss (optional)" style={{width:"100%", padding:12, marginBottom:12, borderRadius:8, border:"1px solid #ccc"}} />
          <button onClick={openPos} style={{width:"100%", padding:16, background:"#0f3460", color:"white", borderRadius:12, fontWeight:"bold"}}>OPEN {side}</button>
        </div>
      )}
      {tab==="positions" && <div>Transactions list — fetch /api/positions</div>}
      {tab==="profile" && <div>Profile stats — fetch /api/profile</div>}
    </div>
  )
}
