# Analiza dryfów ML — Dokumentacja techniczna
## Panel raportów APT20327 | Cyfrowy Bliźniak TOP-1

**Wersja:** 1.0  
**Data:** 2026-04-16  
**Dotyczy:** Zakładka „Analiza dryfów ML" w widoku `/raporty`

---

## 1. Cel i zakres

Moduł analizy dryfów ML służy do **wczesnego wykrywania degradacji parametrów procesu** na linii produkcyjnej APT20327 — zanim degradacja przełoży się na braki jakościowe lub awarię maszyny.

Zakres monitorowania obejmuje:
- Prasę hydrauliczną (siła nacisku, czasy hydrauliczne)
- Sekcję chłodzenia (8 kanałów woda/ciśnienie/przepływ)
- Temperatury załadunku i wyładunku detalu
- Atmosferę pieca (punkt rosy, suche powietrze)
- Czas wygrzewania (soak time)
- Pirometry i detektory temperaturowe

Dane wejściowe: **103 386 cykli produkcyjnych** z okresu 2025-06-15 do dziś, z czego **69 355 cykli** objętych pełną analizą cech fizycznych.

---

## 2. Filozofia podejścia — „najpierw fizyka, potem ML"

Platforma nie przekazuje surowych wartości sensorów bezpośrednio do modelu matematycznego. Przyjęta zasada brzmi:

> **Liczymy cechy fizyczne wynikające z inżynierii procesu (warstwa 1), dopiero na tych cechach uruchamiamy algorytmy ML.**

Podejście to ma trzy uzasadnienia:

1. **Odporność na zmianę receptury** — każda receptura MLF (np. Daimler 1500-1900, Volvo 589, VW 001) ma inne wartości nominalne parametrów. Surowe wartości sensorów nie są porównywalne między recepturami; odchylenia od punktu zadanego (SP) — tak.

2. **Interpretowalność wyników** — wykryty dryf ma bezpośrednie fizyczne znaczenie (np. „przepływ kanału chłodzącego upper-32 odchyla się od zadanego o +2σ"), a nie tylko abstrakcyjny „score anomalii".

3. **Mniej danych potrzebnych do treningu** — cechy fizyczne zachowują znaczenie nawet przy małej liczbie próbek nowej receptury.

---

## 3. Architektura trzywarstwowa

```
Cykl produkcyjny (csv_process_log)
          │
          ▼
┌─────────────────────────────────────┐
│  WARSTWA 1: Cechy fizyczne          │
│  apt_app.ml_features_v1             │
│  ~50 cech per cykl                  │
└──────────────┬──────────────────────┘
               │
   ┌───────────┼────────────┐
   ▼           ▼            ▼
Z-score     EWMA         Isolation
per recipe  per cecha    Forest
(aktywny)   (planowany)  (planowany)
   │
   ▼
Alerty DRIFT_ZSCORE w tabeli alerts
Wykresy w zakładce „Analiza dryfów ML"
```

Aktualnie wdrożona i działająca jest **Warstwa Z-score**. EWMA i Isolation Forest na cechach fizycznych są zaplanowane jako kolejne etapy.

---

## 4. Warstwa 1 — Cechy fizyczne

Każda cecha to transformacja surowych wartości sensorów na wielkość mierzącą **odchylenie od normy procesu**. Poniżej opisane są grupy cech z uzasadnieniem wyboru formuły.

---

### 4.1 Prasa hydrauliczna

Dane dostępne dla **69 365 cykli** (pełne okno historyczne).

| Cecha | Formuła | Jednostka |
|-------|---------|-----------|
| `press_force_dev` | `press_force_pv − press_force_sp` | N |
| `press_force_ratio` | `press_force_pv / press_force_sp` | — |
| `closing_time_dev` | `closing_time_pv − closing_time_sp` | s |
| `closing_time_ratio` | `closing_time_pv / closing_time_sp` | — |
| `dwell_time_dev` | `dwell_time_pv − dwell_time_sp` | s |
| `press_energy_proxy` | `press_force_pv × closing_time_pv` | N·s |

**Uzasadnienie formuł:**

- **Odchylenie od SP (dev)** zamiast wartości absolutnej: każda receptura ma inny nominał siły nacisku (np. 7 024 N dla Daimler 1500-1900, 10 035 N dla Volvo 589). Porównanie absolutnych wartości między recepturami byłoby nonsensem — odchylenie od zadanego punktu jest natomiast zawsze zerocentrowane i porównywalne.

- **Ratio (pv/sp)** jako cecha równoległa: pozwala Z-score działać niezależnie od skali — „1.02" oznacza 2% przekroczenie niezależnie od tego, czy SP wynosi 7 000 N czy 10 000 N. Używane przez algorytm Isolation Forest (planowany), gdzie cechy powinny być unormowane.

- **press_energy_proxy = siła × czas zamykania**: to proxy energii mechanicznej cyklu hydraulicznego. Jeśli pompa zaczyna się zużywać, siłownik „ciągnie" dłużej przy tej samej sile (lub generuje mniejszą siłę przy tym samym czasie) — obie te zmiany są widoczne jako wzrost `press_energy_proxy`, nawet gdy każda z dwóch składowych osobno mieści się jeszcze w tolerancji.

**Dlaczego prasa jest priorytetem:** czas zamykania `closing_time_pv` ma w danych rzeczywistych odchylenie standardowe σ = 0,004 s przy średniej 1,502 s (dla receptury Daimler 1500-1900). To wyjątkowo czysty sygnał — nawet dryft o 0,02 s (5σ) jest jednoznacznym symptomem degradacji hydrauliki, a nie szumem pomiarowym.

---

### 4.2 Kanały chłodzące (sekcja a10)

8 kanałów: 4 górne (upper 32, 34, 42, 44) i 4 dolne (lower 12, 14, 22, 24). Dane dla **69 365 cykli**.

| Cecha | Formuła | Jednostka |
|-------|---------|-----------|
| `cool_temp_dev_uXX` | `temp_pv − temp_sp` | °C |
| `cool_flow_dev_uXX` | `flow_pv − flow_sp` | l/min |
| `cool_press_dev_uXX` | `pressure_pv − pressure_sp` | bar |
| `cool_Q_approx_uXX` | `flow_pv × (temp_pv − temp_sp)` | l·°C/min |
| `cool_upper_flow_mean` | `mean(flow_u32, u34, u42, u44)` | l/min |
| `cool_lower_flow_mean` | `mean(flow_l12, l14, l22, l24)` | l/min |
| `cool_flow_imbalance` | `upper_mean / lower_mean` | — |
| `cool_temp_spread` | `STDDEV(8 × temp_pv)` | °C |
| `cool_press_spread` | `STDDEV(8 × pressure_pv)` | bar |

**Uzasadnienie formuł:**

- **Odchylenie temperatury od SP (delta_T = pv − sp)** zamiast temperatury absolutnej: temperatura wody wchodzącej do kanałów jest sezonowo zmienna (zimą niższa, latem wyższa). Absolutna wartość pv zawierałaby ten szum sezonowy i generowała fałszywe alarmy. Odchylenie od SP eliminuje wpływ temperatury zasilania — mierzymy co układ chłodzący robi w stosunku do tego czego od niego oczekujemy.

- **cool_Q_approx = przepływ × delta_T**: to proxy mocy chłodniczej (analogia do Q = m·cp·ΔT z termodynamiki, gdzie m jest proporcjonalne do przepływu, a ΔT to właśnie odchylenie temperatury od SP). Spadek `cool_Q` przy stabilnym przepływie oznacza, że kanał nie odbiera ciepła tak jak powinien — np. z powodu inkrustacji lub powietrznej kieszeni.

- **cool_flow_imbalance = górne/dolne**: nominalna wartość to ~1,0 (symetria układu). Zmiana tego współczynnika wskazuje na nierównomierność zużycia lub selektywne blokowanie kanałów — niemożliwe do wykrycia patrząc na każdy kanał osobno.

- **cool_temp_spread i cool_press_spread (STDDEV po 8 kanałach)**: rozrzut między kanałami rośnie gdy jeden lub kilka zaczyna odbiegać od pozostałych. To czulszy wskaźnik nierównomierności niż max-min, bo nie jest wrażliwy na pojedynczą wartość odstającą.

---

### 4.3 Temperatura załadunku i wyładunku

Dane dla **103 386 cykli** (pełna historia).

| Cecha | Formuła | Jednostka |
|-------|---------|-----------|
| `temp_load_dev_c` | `temp_at_loading_pv − temp_at_loading_sp` | °C |
| `temp_unload_dev_c` | `temp_at_unloading_pv − temp_at_unloading_sp` | °C |
| `temp_drop_c` | `temp_at_loading_pv − temp_at_unloading_pv` | °C |
| `temp_load_vs_sp_ratio` | `temp_at_loading_pv / temp_at_loading_sp` | — |
| `dewpoint_load_dev` | `dewpoint_load_pv − dewpoint_load_sp` | °C |
| `dewpoint_unload_dev` | `dewpoint_unload_pv − dewpoint_unload_sp` | °C |
| `dewpoint_gradient` | `dewpoint_unload_pv − dewpoint_load_pv` | °C |
| `soak_time_dev_s` | `soak_time_pv − soak_time_sp` | s |

**Uzasadnienie formuł:**

- **temp_drop_c = załadunek − wyładunek**: różnica temperatur w trakcie cyklu jest bezpośrednim wskaźnikiem skuteczności chłodzenia ramki. Maleje gdy: chłodzenie jest mniej efektywne, czas cyklu jest krótszy, lub temperatura wody chłodzącej rośnie ponad normę.

- **dewpoint_gradient = wyładunek − załadunek**: wzrost punktu rosy w trakcie cyklu (wartość dodatnia i rosnąca) jest wczesnym symptomem nieszczelności komory atmosferycznej lub utraty jakości gazu ochronnego. Kluczowe dla zapobiegania utlenianiu detali.

- **soak_time_dev_s**: czas wygrzewania jest sterowany przez regulator PID pieca. Odchylenia od SP wskazują na niestabilność pętli sterowania — np. zużycie elementów grzejnych lub dryf czujnika temperatury.

---

### 4.4 Strefy grzewcze (6 stref)

Dane dla **7 742 cykli** (dostępne od ~2026-02).

| Cecha | Formuła | Jednostka |
|-------|---------|-----------|
| `zone_spread_load` | `MAX(6 stref) − MIN(6 stref)` przy załadowaniu | °C |
| `zone_std_load` | `STDDEV(6 stref)` przy załadowaniu | °C |
| `zone_dev_ul/uc/ur/ll/lc/lr_load` | `temp_pv_strefa − temp_sp` | °C |
| `zone_spread_unload` | `MAX − MIN(6 stref)` przy wyładowaniu | °C |
| `thermal_stability_30s` | `MEAN(strefy_60s) − MEAN(strefy_30s)` | °C |

**Uzasadnienie formuł:**

- **zone_spread i zone_std**: gradient temperatury między strefami komory rośnie gdy jeden element grzejny zaczyna tracić moc lub cyrkulacja jest zaburzona. STDDEV jest czulszy niż max-min — nie jest podatny na jednorazową wartość odstającą.

- **thermal_stability_30s**: mierzy czy temperatura nadal rośnie w ostatnich 30 sekundach przed końcem cyklu. Wartość ujemna = temperatura stabilizuje się (poprawnie). Wartość dodatnia = detal wciąż się nagrzewa = cykl jest za krótki lub piec za słaby.

---

### 4.5 Pirometry i detektory

Dane dla **98 548 cykli**.

| Cecha | Formuła | Jednostka |
|-------|---------|-----------|
| `pyh_dev` | `pyh_act_temp − pyh_target` | °C |
| `ha_spread` | `MAX(ha_tempdet_1..4) − MIN(ha_tempdet_1..4)` | °C |
| `ha_mean_dev` | `MEAN(ha_tempdet_1..4) − ha_target` | °C |
| `pyc_dev` | `pyc_act_temp − pyc_target` | °C |
| `ca_spread` | `MAX(ca_tempdet_1..4) − MIN(ca_tempdet_1..4)` | °C |

**Uzasadnienie:** pirometry mierzą temperaturę detalu przez okno optyczne. Dryft `pyh_dev` lub `pyc_dev` może oznaczać zanieczyszczenie okna (zmiana emisyjności), co jest trudne do wykrycia innymi metodami. `ha_spread` i `ca_spread` (rozrzut między detektorami) wykrywa nierównomierność dyszy gorącego/zimnego powietrza.

---

## 5. Model Z-score — algorytm i parametry

### 5.1 Formuła

```
z = (wartość_cyklu − mean_N) / std_N

gdzie:
  mean_N = średnia arytmetyczna z ostatnich 500 cykli TEJ SAMEJ receptury
  std_N  = odchylenie standardowe z tych samych 500 cykli
```

Obliczenie jest realizowane jako **rolling window function** w PostgreSQL:

```sql
AVG(cecha) OVER (
  PARTITION BY recipe_mlf
  ORDER BY date_time
  ROWS BETWEEN 499 PRECEDING AND CURRENT ROW
)
```

### 5.2 Dlaczego N = 500 cykli (okno historii)

Wybór okna 500 cykli jest kompromisem między dwoma wymaganiami:

- **Okno za małe (np. 50 cykli):** baseline jest zbyt wrażliwy — 5 kolejnych cykli z nieco wyższą temperaturą przesuwa „normę" i maskuje dryft.
- **Okno za duże (np. 5000 cykli):** system reaguje zbyt późno — dryft narastający przez 300 cykli jest już wchłonięty w średnią i niewidoczny.

500 cykli odpowiada **ok. 2–4 godzinom produkcji** przy typowej wydajności linii. Jest wystarczające aby baseline był statystycznie stabilny (centralny limit twierdzenia działa od ~30 obserwacji), a jednocześnie wrażliwe na zmiany w skali godzin, a nie dni.

### 5.3 Dlaczego per receptura (recipe_mlf)

Bez segmentacji per receptura Z-score byłby bezużyteczny. Przykład z danych rzeczywistych:

| Receptura | press_force_avg | press_force_std |
|-----------|----------------|----------------|
| 441345100 Daimler 1500-1900 | 7 024 N | 77 N |
| 441354200 Daimler 6700-7200 | 8 087 N | 129 N |
| 441543200 Volvo 589-1 | 10 035 N | 11 N |
| 441064700 VW 001 | 8 108 N | 222 N |

Cykl Volvo z siłą 10 000 N obliczony na baselinie Daimler (avg=7024, std=77) dałby z = (10000-7024)/77 = **38,7σ** — fałszywy alarm krytyczny przy każdej zmianie programu.

### 5.4 Progi alarmowe i uzasadnienie statystyczne

| Próg | Warunek | Poziom | % fałszywych alarmów (rozkład normalny) |
|------|---------|--------|----------------------------------------|
| WARNING | \|z\| > 3 | DRIFT_WARNING | 0,27% — ok. 1 na 370 cykli |
| CRITICAL | \|z\| > 4 | DRIFT_CRITICAL | 0,006% — ok. 1 na 15 787 cykli |

Progi 3σ i 4σ to standard w statystycznej kontroli jakości (Statistical Process Control, SPC). Próg 3σ jest analogiczny do granic kontrolnych na kartach Shewharta stosowanych w przemyśle od dziesięcioleci.

Celowo **nie wybrano progu 2σ** (który wyklucza 4,6% populacji) — generowałby zbyt wiele alarmów operacyjnych i prowadziłby do „zmęczenia alertami".

### 5.5 Wyniki pierwszego uruchomienia (baseline 2026-04-16)

Na zbiorze 69 337 cykli:

| Cecha | Alerty >3σ | Alerty >4σ | Interpretacja wstępna |
|-------|-----------|-----------|----------------------|
| `cool_flow_u32` | 1 731 | — | Przepływ kanału upper-32 — najczęstsze odchylenia |
| `temp_load` | 1 372 | 154 | Temperatura załadunku — możliwe wahania sezonowe |
| `press_force` | 1 069 | 537 | Siła prasy — 537 krytycznych wymaga analizy |
| `dewpoint` | 710 | — | Punkt rosy — zmienność atmosfery |
| `cool_press_u33` | 516 | — | Ciśnienie kanału upper-33 |
| `closing_time` | 0 | 0 | Czas zamykania — bardzo stabilny, brak dryfów |

Stabilność `closing_time` (0 alertów) potwierdza poprawność metodologii: jest to parametr dobrze kontrolowany przez układ hydrauliczny i wynik „zero anomalii" jest oczekiwany.

---

## 6. Automatyczna aktualizacja — Cron

System aktualizuje analizę dryfów **automatycznie co 15 minut** bez interwencji operatora.

**Harmonogram:** `/etc/cron.d/gedia-drift` — `*/15 * * * *`

**Co dzieje się przy każdym wywołaniu:**

1. Sprawdzenie ile cykli z `csv_process_log` nie ma jeszcze w `ml_features_v1`
2. Jeśli 0 — zakończenie w ~80ms (brak obciążenia serwera)
3. INSERT cech Warstwy 1 dla maksymalnie 2000 nowych cykli
4. UPDATE Z-score (rolling 500 per receptura) dla nowo dodanych wierszy
5. UPDATE `zscore_max` (najwyższy |z| spośród wszystkich cech w danym cyklu)

Limit 2000 cykli per wywołanie (~30 minut produkcji) zapewnia, że nawet po przerwie w działaniu serwera aktualizacja dogoni zaległości w ciągu kilku interwałów.

**Bezpieczeństwo:** endpoint crona (`/api/internal/drift-compute`) jest wyłączony z ochrony sesji NextAuth — uwierzytelniany jest natomiast nagłówkiem `X-Cron-Secret` przechowywanym w zmiennych środowiskowych serwera.

---

## 7. Widok w panelu — zakładka „Analiza dryfów ML"

Zakładka dostępna w menu **Raporty → Analiza dryfów ML**.

**Filtry dostępne dla operatora:**
- **Receptura** — lista z `ml_features_v1` (tylko receptury z danymi)
- **Okno czasowe** — 24H / 2D / 4D / 7D / 30D
- **Próg σ** — zmienny próg alarmowy (domyślnie 3)

**Wyświetlane dane:**
- Wykres Z-score w czasie dla wybranej cechy / receptury
- KPI summary: ile cech przekroczyło próg w wybranym oknie
- Tabela najnowszych alertów z wartością Z i datą cyklu

**Endpointy API obsługujące widok:**

| Endpoint | Opis |
|----------|------|
| `GET /api/ml/drift` | Seria Z-score dla wybranej receptury, okna i progu |
| `GET /api/ml/drift/recipes` | Lista receptur z danymi w `ml_features_v1` |
| `POST /api/internal/drift-compute` | (Cron) Przeliczenie nowych cykli |

---

## 8. Planowane rozszerzenia

### Etap 2 — EWMA (Exponentially Weighted Moving Average)

EWMA wykrywa **powolny dryft** którego Z-score nie widzi, bo dryft wchłaniany jest stopniowo w okno historii.

Formuła:
```
EWMA_t = λ × wartość_t + (1 − λ) × EWMA_{t−1}
UCL    = EWMA_t + k × σ₀ × sqrt(λ / (2 − λ))

λ = 0,1  (waga nowego pomiaru — "pamięć" ~10 ostatnich cykli silnie, ~50 słabo)
k = 3    (szerokość pasma kontrolnego)
σ₀       = odchylenie z okresu bazowego (stabilna produkcja bez dryfów)
```

Przykład zastosowania: `closing_time_pv` rośnie o 0,001 s co 100 cykli. Po 500 cyklach każdy jednotkowy cykl mieści się w 3σ Z-score (0,005 s < 0,012 s = 3×0,004), ale EWMA zsumuje trend i wyjdzie poza UCL.

### Etap 3 — Isolation Forest na cechach Layer 1

Zamiast uruchamiać Isolation Forest na surowych wartościach sensorów (16 sygnałów), model będzie trenowany na wektorze **~50 cech fizycznych** z `ml_features_v1`.

Korzyść: wykrywanie anomalii wielowymiarowych — np. ciśnienie hydrauliczne normalne + przepływ normalny + temperatura nieznacznie wyższa = razem kombinacja anomalna, wskazująca na konkretny mechanizm degradacji.

### Etap 4 — Energia elektryczna per cykl

JOIN między `csv_process_log` a danymi analizatorów energii (`apt4_log.tlog_apt4edata`) po oknie czasowym cyklu, dodanie cech energetycznych do `ml_features_v1` (Wh na cykl per komponent: termika, wygrzewanie, chłodzenie, prasa).

Umożliwi wskaźnik `energy_per_force = Wh_prasy / press_force_pv` — efektywność energetyczna prasy. Wzrost oznacza degradację hydrauliki.

---

## 9. Podsumowanie

| Aspekt | Wartość |
|--------|---------|
| Pokrycie danych | 103 386 cykli (od 2025-06-15) |
| Cykli z cechami ML | 69 355 |
| Liczba cech fizycznych | ~50 (grupy A–E) |
| Aktywny model | Z-score, rolling 500 cykli per receptura |
| Progi | WARNING: \|z\| > 3σ, CRITICAL: \|z\| > 4σ |
| Aktualizacja | Co 15 minut (cron automatyczny) |
| Alerty wykryte (baseline) | 5 501 WARNING, 1 872 CRITICAL |
| Planowane modele | EWMA (dryft powolny), Isolation Forest (anomalie wielowymiarowe) |
