"""
NLST (Netlist, Inc.) — sum-of-parts valuation, as of 2026-08-17.

Price/share and share count are the only market inputs; everything else is
built from reported figures or stated deal terms.

The framing that matters: NLST is not a memory-module company with a lawsuit.
It is a licensing campaign against the three DRAM makers, wrapped around a
low-margin resale business. Samsung is signed. SK hynix and Micron are not.
So the value is mostly a probability-weighted campaign, and the honest output
is not a price target but a back-solve of what $5.88 requires.

  A. Samsung license     — SIGNED 2026-08-05, contractual, formula/refund risk
  B. Micron judgment     — $445M final judgment, IPR-appeal contingent
  C. SK hynix renewal    — 2021 pact EXPIRED Apr-2026, unrenewed, open
  D. Micron/OEM campaign — ITC + CDCA filed Aug-2026, DDR5, early stage
  E. Product/resale      — cyclical DRAM beta, ~80% resale, thin margin

Run: python3 analysis/nlst_dcf.py
"""

# ---- market inputs ---------------------------------------------------------
PRICE          = 5.88     # OTCQB close 2026-08-14, jhirgit/daily-prices intraday.json
SHARES_Q2      = 333.894  # millions, issued+outstanding 2026-06-27 (10-Q)
SHARES_SAMSUNG = 10.0     # millions, Samsung equity purchase, 5yr lock-up
SHARES         = SHARES_Q2 + SHARES_SAMSUNG
CASH           = 30.655   # $M at 2026-06-27
RESTRICTED     = 10.000   # $M
DEBT           = 0.0      # no material funded debt identified (UNVERIFIED — EDGAR blocked)

# ---- A. Samsung, signed ----------------------------------------------------
SS_UPFRONT_NET = 200.0    # $239M gross less Korean withholding
SS_QTR_NET_CAP = 27.5     # $M/qtr net cap ($32.9M gross), 20 quarters
SS_QUARTERS    = 20
R_SIGNED       = 0.11     # strong counterparty; risk is the revenue formula, not credit

# ---- benchmark: the Samsung deal as a $/DRAM-share anchor ------------------
# Samsung ~40% DRAM share -> $897M gross / 5yr. Scale peers off that rate.
SS_GROSS       = 897.0
SHARE_SS, SHARE_HYNIX, SHARE_MU = 0.40, 0.36, 0.22
NET_OF_GROSS_KR = 0.836   # 750/897, Korean withholding
NET_OF_GROSS_US = 0.90    # Micron is domestic; less withholding leakage

HYNIX_GROSS = SS_GROSS * SHARE_HYNIX / SHARE_SS      # ~807
MU_GROSS    = SS_GROSS * SHARE_MU    / SHARE_SS      # ~493

# ---- B. Micron judgment ----------------------------------------------------
MICRON_AWARD  = 445.0     # final judgment Jul-2024; Fed Cir rejected Micron appeal Mar-2026
MICRON_YEARS  = 1.75
R_CONTESTED   = 0.15

# ---- C/D. unsigned campaigns ----------------------------------------------
R_CAMPAIGN    = 0.18      # unsigned, litigation-contingent, timing-uncertain

# ---- E. product business ---------------------------------------------------
Q2_REV          = 109.8
OPEX_EX_LEGAL_Q = 4.7     # $21.5M opex less $16.8M IP legal
LEGAL_Q         = 16.8    # elevated while Micron actions are live
R_PRODUCT       = 0.13
TAX             = 0.15    # blended cash rate; large NOLs shelter near-term


def pv_samsung(realization, r=R_SIGNED):
    """Upfront is contractual, taken at 100%. Quarterlies are formula-linked
    and capped, so `realization` is the fraction of cap actually earned."""
    pv = SS_UPFRONT_NET / (1 + r) ** 0.12
    for q in range(1, SS_QUARTERS + 1):
        pv += (SS_QTR_NET_CAP * realization) / (1 + r) ** (0.25 * q + 0.12)
    return pv


def pv_micron_judgment(prob, r=R_CONTESTED):
    return prob * MICRON_AWARD / (1 + r) ** MICRON_YEARS


def pv_campaign(gross, prob, realization, years_to_deal, net_ratio, r=R_CAMPAIGN):
    """A hypothetical peer deal on Samsung terms: 20% of value upfront at
    signing, remainder spread over 20 quarters thereafter."""
    net = gross * net_ratio * realization
    upfront, stream = net * 0.20, net * 0.80
    pv = upfront / (1 + r) ** years_to_deal
    for q in range(1, 21):
        pv += (stream / 20) / (1 + r) ** (years_to_deal + 0.25 * q)
    return prob * pv


def pv_product(gm, rev_decay, legal_years=2.0, g=0.0, r=R_PRODUCT, years=10):
    """rev_decay applies once, in year 2, for DRAM cycle mean-reversion."""
    rev, pv = Q2_REV * 4, 0.0
    for y in range(1, years + 1):
        if y == 2:
            rev *= (1 - rev_decay)
        elif y > 2:
            rev *= (1 + g)
        legal = LEGAL_Q * 4 if y <= legal_years else 12.0
        ebit = rev * gm - OPEX_EX_LEGAL_Q * 4 - legal
        pv += (ebit * (1 - TAX) if ebit > 0 else ebit) / (1 + r) ** y
    ebit_t = rev * gm - OPEX_EX_LEGAL_Q * 4 - 12.0
    fcf_t = ebit_t * (1 - TAX) if ebit_t > 0 else ebit_t
    tv = fcf_t * (1 + g) / (r - g) if r > g else 0.0
    return pv + tv / (1 + r) ** years


#                 ss_realz  mu_judg_p  hyx_p hyx_realz hyx_yr  mu_p  mu_realz mu_yr   gm   decay
SCEN = {
    "Bear": (0.45, 0.20, 0.30, 0.35, 1.5, 0.20, 0.40, 3.0, 0.14, 0.45),
    "Base": (0.70, 0.45, 0.55, 0.60, 1.0, 0.45, 0.65, 2.5, 0.18, 0.30),
    "Bull": (0.92, 0.70, 0.75, 0.85, 0.6, 0.65, 0.90, 2.0, 0.21, 0.12),
}

print(f"NLST sum-of-parts | ${PRICE:.2f} | {SHARES:.1f}M sh | "
      f"mkt cap ${PRICE * SHARES / 1000:.2f}B")
print(f"peer benchmark off Samsung: SK hynix ${HYNIX_GROSS:.0f}M gross, "
      f"Micron ${MU_GROSS:.0f}M gross\n")

cols = ("Samsung", "MU judg", "SKhynix", "MU camp", "Product", "Cash", "Equity", "/sh")
print(f"{'':6}" + "".join(f"{c:>9}" for c in cols) + f"{'vs mkt':>9}")
print("-" * 87)

for name, (ssr, mjp, hp, hr, hy, mp, mr, my, gm, dec) in SCEN.items():
    a = pv_samsung(ssr)
    b = pv_micron_judgment(mjp)
    c = pv_campaign(HYNIX_GROSS, hp, hr, hy, NET_OF_GROSS_KR)
    d = pv_campaign(MU_GROSS, mp, mr, my, NET_OF_GROSS_US)
    e = pv_product(gm, dec)
    f = CASH + RESTRICTED - DEBT
    eq = a + b + c + d + e + f
    ps = eq / SHARES
    print(f"{name:6}" + "".join(f"{v:>9.0f}" for v in (a, b, c, d, e, f, eq))
          + f"{ps:>9.2f}{(ps / PRICE - 1) * 100:>8.0f}%")

print("-" * 87)
print(f"{'Market':6}" + " " * 54 + f"{PRICE * SHARES:>9.0f}{PRICE:>9.2f}")

# ---- implied expectations --------------------------------------------------
base = SCEN["Base"]
signed_and_owned = (pv_samsung(base[0]) + pv_micron_judgment(base[1])
                    + pv_product(base[8], base[9]) + CASH + RESTRICTED)
gap = PRICE * SHARES - signed_and_owned
print(f"\nIMPLIED EXPECTATIONS at ${PRICE:.2f}")
print(f"  Signed Samsung + Micron judgment + product + cash (Base) = ${signed_and_owned:>7.0f}M")
print(f"  Market capitalization                                    = ${PRICE * SHARES:>7.0f}M")
print(f"  Residual the market assigns to UNSIGNED campaigns        = ${gap:>7.0f}M")

hyx_max = pv_campaign(HYNIX_GROSS, 1.0, 1.0, 1.0, NET_OF_GROSS_KR)
mu_max = pv_campaign(MU_GROSS, 1.0, 1.0, 2.5, NET_OF_GROSS_US)
print(f"\n  PV of SK hynix at 100% probability AND 100% of cap       = ${hyx_max:>7.0f}M")
print(f"  PV of Micron campaign at 100% AND 100% of cap            = ${mu_max:>7.0f}M")
print(f"  Both, fully certain, fully realized                      = ${hyx_max + mu_max:>7.0f}M")
covered = (hyx_max + mu_max) / gap * 100
print(f"  -> perfect execution on both covers {covered:.0f}% of the residual")
if covered < 100:
    print("     The residual EXCEEDS the theoretical maximum of both campaigns.")

print("\nSENSITIVITY — $/share, SK hynix probability x Micron-campaign probability")
print("(Samsung 70% of cap, MU judgment 45%, product Base; both peers at 65% of cap)\n")
mu_probs = (0.2, 0.35, 0.5, 0.65, 0.8)
hdr = "P(hynix) \\ P(MU)"
print(f"{hdr:>17}" + "".join(f"{p:>9.0%}" for p in mu_probs))
for hp in (0.2, 0.35, 0.5, 0.65, 0.8, 1.0):
    row = f"{hp:>16.0%} "
    for mp in mu_probs:
        eq = (pv_samsung(0.70) + pv_micron_judgment(0.45)
              + pv_campaign(HYNIX_GROSS, hp, 0.65, 1.0, NET_OF_GROSS_KR)
              + pv_campaign(MU_GROSS, mp, 0.65, 2.5, NET_OF_GROSS_US)
              + pv_product(0.18, 0.30) + CASH + RESTRICTED)
        row += f"{eq / SHARES:>9.2f}"
    print(row)
print(f"\n(every cell below ${PRICE:.2f} = overvalued on these assumptions)")


# ---- residual beyond the theoretical maximum of every identified campaign ---
resid_after_max = gap - (hyx_max + mu_max)
print(f"\nRESIDUAL BEYOND ALL IDENTIFIED CAMPAIGNS AT FULL REALIZATION")
print(f"  Residual over signed assets                = ${gap:>7.0f}M")
print(f"  less SK hynix + Micron campaigns at 100%   = ${hyx_max + mu_max:>7.0f}M")
print(f"  = value with NO identified source          = ${resid_after_max:>7.0f}M "
      f"({resid_after_max / (PRICE * SHARES) * 100:.0f}% of market cap)")
print("  This is the HBM/DDR5-enforceability bet. It is not in any signed")
print("  contract, any judgment, or any scaled peer deal.")

# ---- probability tree ------------------------------------------------------
BANDS = {"Bear": (1.25, 2.25, 0.30), "Base": (2.75, 4.00, 0.45),
         "Bull": (6.50, 9.50, 0.25)}
print("\nPROBABILITY TREE")
ev = 0.0
for name, (lo, hi, p) in BANDS.items():
    mid = (lo + hi) / 2
    ev += p * mid
    print(f"  {name:5} ${lo:>5.2f}-${hi:<5.2f} mid ${mid:>5.2f}  p={p:.0%}")
print(f"\n  Tree EV = ${ev:.2f} vs ${PRICE:.2f} spot = {(ev / PRICE - 1) * 100:+.0f}%")
up = (BANDS['Bull'][0] + BANDS['Bull'][1]) / 2 / PRICE - 1
dn = (BANDS['Bear'][0] + BANDS['Bear'][1]) / 2 / PRICE - 1
print(f"  Upside to bull mid {up * 100:+.0f}% / downside to bear mid {dn * 100:+.0f}%")
print(f"  Magnitude skew {abs(up) / abs(dn):.2f}:1 "
      f"({'favourable' if abs(up) > abs(dn) else 'UNFAVOURABLE'})")
