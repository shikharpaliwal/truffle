from truffle.universe import classify

CFG = {"exclusions": {
    "symbols": ["USDT", "XAUT"],
    "name_contains": ["wrapped", "binance-peg"],
    "wrapper_prefixes": ["W", "ST", "CB"],
    "peg_tolerance": 0.03,
}}


def mk(cid, sym, name, price=100.0):
    return {"id": cid, "symbol": sym.lower(), "name": name, "current_price": price}


def run(rows, cats=None):
    return {r["id"]: r["exclusion_reason"] for r in classify(rows, CFG, cats or {})}


def test_category_membership_wins():
    out = run([mk("usd-coin", "USDC", "USDC", 1.0)], {"usd-coin": "category:stablecoins"})
    assert out["usd-coin"] == "category:stablecoins"


def test_symbol_blocklist():
    assert run([mk("tether", "USDT", "Tether", 1.0)])["tether"] == "symbol_blocklist"


def test_name_blocklist():
    assert run([mk("wbtc", "XYZ", "Wrapped Bitcoin")])["wbtc"] == "name_blocklist"


def test_wrapper_prefix_needs_base_in_universe():
    rows = [mk("ethereum", "ETH", "Ethereum"), mk("weth", "WETH", "W-Ether"),
            mk("waves", "WAVES", "Waves")]
    out = run(rows)
    assert out["weth"] == "wrapper_prefix:W"
    assert out["waves"] is None          # AVES is not a coin, so WAVES survives
    assert out["ethereum"] is None


def test_peg_price_heuristic():
    rows = [mk("some-usd", "XUSD", "Some Dollar", 1.004), mk("far", "YUSD", "Off Peg", 0.55)]
    out = run(rows)
    assert out["some-usd"] == "peg_price"
    assert out["far"] is None            # broken peg is not a stablecoin we should hide


def test_normal_coins_survive():
    rows = [mk("solana", "SOL", "Solana"), mk("chainlink", "LINK", "Chainlink"),
            mk("stellar", "XLM", "Stellar")]
    assert all(v is None for v in run(rows).values())


def test_included_flag_matches_reason():
    rows = [mk("tether", "USDT", "Tether", 1.0), mk("solana", "SOL", "Solana")]
    out = {r["id"]: r["included"] for r in classify(rows, CFG, {})}
    assert out == {"tether": False, "solana": True}
