# Model Parameters Reference

Parameters used in `simulator/cooling_tower.py`, with physical justification and literature sources.

---

## Heat transfer — NTU-effectiveness method

### `KAV_L_NOMINAL = 1.8`

| | |
|---|---|
| Units | dimensionless |
| Symbol | KaV/L |

The tower characteristic at design conditions. Combines the mass transfer coefficient *Ka* [kg/m²·s], the active packing volume *V* [m³], and the water mass flow rate *L* [kg/s] into a single dimensionless group that describes how much heat/mass transfer capacity the packing provides per unit of water flow.

Typical packed cooling towers fall in the range 1.2–2.5 depending on packing type and depth. A value of 1.8 is representative of modern film-fill packing at design L/G.

> Merkel, F. (1925). *Verdunstungskühlung*. VDI Forschungsarbeiten, No. 275, Berlin.
> Kröger, D.G. (2004). *Air-Cooled Heat Exchangers and Cooling Towers*, Vol. 2, Ch. 9. PennWell.

---

### `LG_EXPONENT = -0.6`

| | |
|---|---|
| Units | dimensionless |
| Symbol | n in KaV/L ∝ (L/G)ⁿ |

NTU varies with the water-to-air mass flow ratio (L/G) raised to this power:

```
NTU = KaV/L_nominal × fouling_factor × (L/G)^(-0.6)
```

The exponent −0.6 comes from empirical correlations for film-fill packing. It means increasing airflow (lowering L/G) improves NTU, but with diminishing returns. Values in the literature range from −0.5 to −0.8 depending on packing geometry.

> Fills, B., Kröger, D.G. (2000). "Influence of inlet flow loss coefficients on cooling tower performance." *Heat Transfer Engineering*, 21(6), 29–35.
> Osterle, J.F. (1991). "On the analysis of counter-flow cooling towers." *International Journal of Heat and Mass Transfer*, 34(4–5), 1313–1317.

---

### `FAN_EXPONENT = 0.8`

| | |
|---|---|
| Units | dimensionless |
| Symbol | m in G ∝ speed^m |

Air mass flow rate scales with fan speed by the fan affinity law:

```
G = G_design × (fan_speed_pct / 100)^0.8
```

A pure affinity law (ideal axial fan, no system curve interaction) gives an exponent of 1.0. The value 0.8 accounts for the non-linear relationship between fan speed and actual volumetric delivery when operating against a static pressure. Values of 0.75–0.85 are commonly used in cooling tower simulation literature.

> CTI (Cooling Technology Institute). (2000). *Cooling Tower Performance Curves*. Bulletin PFM-143.
> El-Dessouky, H., Ettouney, H., Al-Juwayhel, F. (1997). "Multiple effect evaporation — vapor compression desalination processes." *Trans IChemE*, 78(A).

---

## Water and air flow

### `G_DESIGN = 45.0 kg/s`

| | |
|---|---|
| Units | kg/s |
| Symbol | G_design |

Design air mass flow rate at 100% fan speed, sized to give a design L/G ratio of approximately 1.1–1.3 at the nominal water flow of 150 m³/hr (L ≈ 41.7 kg/s). A design L/G in the range 0.75–1.5 is typical for counterflow towers.

```
L/G_design = 41.7 / 45.0 ≈ 0.93
```

> Kröger, D.G. (2004). *Air-Cooled Heat Exchangers and Cooling Towers*, Vol. 2, Table 9.1.

---

### `L_DESIGN = 41.7 kg/s`

| | |
|---|---|
| Units | kg/s |
| Symbol | L_design |

Water mass flow rate at nominal 150 m³/hr, assuming liquid density of 1000 kg/m³:

```
L = 150 m³/hr ÷ 3600 s/hr × 1000 kg/m³ = 41.7 kg/s
```

---

## Evaporation

### Evaporation coefficient `0.00085`

| | |
|---|---|
| Units | m³_evap / (m³_water · °C) |

Used in:

```
E = 0.00085 × L_m3hr × (T_hot_in − T_cold_out)
```

Derived from an energy balance: the latent heat required to evaporate a small fraction of the circulating water accounts for the sensible cooling of the bulk stream. At typical tower temperatures (~35°C), the latent heat of vaporisation is approximately 2430 kJ/kg and the specific heat of water is 4.18 kJ/kg·°C, giving a theoretical evaporation fraction per degree of cooling of:

```
e = Cp_water / L_vap ≈ 4.18 / 2430 ≈ 0.00172 kg_evap / kg_water / °C
```

The empirical coefficient 0.00085 m³/m³/°C is roughly half the theoretical value because not all heat transfer occurs via evaporation — a portion is sensible heat transfer to the air stream. The split depends on ambient conditions; 0.00085 is a conservative, widely-cited design value for warm/humid climates.

> ASHRAE Handbook — HVAC Systems and Equipment (2020), Chapter 40: Cooling Towers.
> Willa, J.L. (2005). "Evaporation and drift in cooling towers." *CTI Journal*, 26(2).

---

## Water quality / blowdown

### `DRIFT_FRACTION = 0.0002`

| | |
|---|---|
| Units | m³_drift / m³_circulating |

Drift is entrained water droplets carried out of the tower by the air stream. Modern drift eliminators achieve 0.001–0.005% of circulating flow. A value of 0.02% (0.0002) is conservative and typical for towers equipped with high-efficiency eliminators.

> CTI Standard STD-140 (2012). *Acceptance Test Code for Water Cooling Towers*.

---

### `COC_TARGET = 4.0`

| | |
|---|---|
| Units | dimensionless |
| Symbol | Cycles of Concentration |

The ratio of basin conductivity to makeup water conductivity at which blowdown is triggered:

```
CoC = Cond_basin / Cond_makeup
Blowdown triggered when CoC ≥ CoC_target
```

Operating CoC is a balance between water conservation (higher CoC = less blowdown = less water wasted) and scaling/corrosion risk (higher CoC = more dissolved solids = more fouling and Legionella risk). A target of 4–6 is typical for well-managed industrial towers in areas with moderate makeup water quality.

> ASHRAE Guideline 12-2000: *Minimizing the Risk of Legionellosis Associated with Building Water Systems*.
> Puckorius, P.R., Brooke, J.M. (1991). "A new practical index for calcium carbonate scale prediction in cooling tower systems." *Corrosion*, 47(4).

---

### `BASIN_VOLUME = 50.0 m³`

| | |
|---|---|
| Units | m³ |

Used in the conductivity mass balance to set the time constant for how quickly dissolved solids concentrate in the basin:

```
d(C_basin)/dt = (Q_makeup × C_makeup − Q_blowdown × C_basin) / V_basin
```

50 m³ is representative of a medium industrial tower handling 150 m³/hr of circulating water. A larger basin damps conductivity swings; a smaller basin responds faster to changes in blowdown or makeup quality.

> Perry's Chemical Engineers' Handbook (9th ed.), Section 12: Psychrometry, Evaporative Cooling.
