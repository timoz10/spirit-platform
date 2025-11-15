# Changelog

All notable changes to the kraken-bot project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Trade quality analysis framework** (2025-11-15): Comprehensive system for analyzing MACD cross performance
  - scripts/trade_quality_analysis.py - Detects crosses, simulates trades, classifies quality
  - scripts/filter_effectiveness_on_quality.py - Tests filter configurations on trade populations
  - scripts/create_q1_2025_data.py - Extracts Q1 2025 validation dataset from cloud PostgreSQL
  - Trade classification: Good (>0%), Neutral (0 to -1%), Bad (<-1%)
  - Metrics: P&L, MFE, MAE, capture rate, exit efficiency
- **Q1 2025 validation dataset** (2025-11-15): test_data/xbtusd_60m_q1_2025.csv
  - Period: 2025-01-01 to 2025-03-31
  - Timeframe: 60-minute bars
  - Total bars: 2,184
  - File size: 183KB
  - Source: Cloud PostgreSQL (188.245.98.89)
- **Comprehensive analysis reports** (2025-11-15): 6 reports documenting filter failures
  - Q1_2025_TRADE_QUALITY_REPORT.md (11KB) - Main findings
  - STRATEGY_ITERATION_REPORT_2025-11-15.md - Iteration analysis
  - ML_THRESHOLD_SWEEP_REPORT_2025-11-15.md - ML threshold results
  - ML_GUARD_VALIDATION_RESULTS_2025-11-15.md - Validation results
  - FINAL_STRATEGY_SUMMARY_2025-11-15.md - Strategy summary
  - ML_VS_BASELINE_PERFORMANCE_REPORT_2025-11-15.md - Performance comparison
- **Analysis output datasets** (2025-11-15):
  - outputs/trade_quality_analysis_detailed.csv (21KB) - All 81 Q1 2025 trades analyzed
  - outputs/filter_effectiveness_detailed.csv (12KB) - Filter performance data

### Added
- **ML whipsaw guard integration** (2025-11-14): Production XGBoost guard operational in Spirit bot
  - Fixed prediction API: Changed from predict_proba to DMatrix + predict (native XGBoost)
  - Feature extraction: All 9 features working (close, SMA200, ATR, MACD, signal, histogram, RSI, ADX, volume)
  - Guard performance: 11 high-risk trades blocked at threshold 0.7 with 100% precision
  - Strategy integration: ML strategy selectable via SPIRIT_STRATEGY environment variable
  - Aliases: macd_cross_ml, macd_ml, ml
- **Test dataset** (2025-11-14): xbtusd_15m_4weeks.csv with 2,689 bars for validation
- **Database migration** (2025-11-14): SQLite to PostgreSQL sync (550K rows, zero errors)
  - Gap filled: 2025-08-26 to 2025-11-14 (80 days of current data)
  - Total PostgreSQL rows: 210.5M (database current through Nov 14, 2025)
- **Comprehensive ML documentation** (2025-11-14): 2,543 lines across 4 guides
  - WHIPSAW_MODEL_TRAINING_METHODOLOGY.md (714 lines) - Training process
  - SPIRIT_ML_INTEGRATION_GUIDE.md (842 lines) - Integration guide
  - ML_STRATEGY_USAGE.md (215 lines) - Usage instructions
  - TECHNICAL_DEBT.md (169 lines) - Technical debt tracking
  - BLOG_WORKSTREAM_TRACKER.md (704 lines) - Blog site milestones
- **Blog site workstream** (2025-11-14): Theme and color palette configured
  - Platform: Ghost CMS
  - Status: Planning phase (defining site deliverables)
  - Purpose: Parallel income stream alongside trading bot
- **Cloud infrastructure** (2025-11-13): Migrated to Hetzner Cloud
  - Bot server: CPX42 (8 vCPU, 16GB RAM, 160GB NVMe) at 188.245.209.204
  - PostgreSQL server: CX22 (2 vCPU, 8GB RAM, 80GB NVMe) at 188.245.98.89
  - Location: Nuremberg, Germany
  - Total cost: €28.70/month (~£25/month)
- **Database migration** (2025-11-13): 64GB PostgreSQL database migrated to cloud
  - Compression: 64GB → 5.0GB (92% reduction using pg_dump --compress=9)
  - Transfer: 3.5 minutes (23 MB/s)
  - Restore: ~3 hours (194M rows, 57 indexes, parallel workers)
  - Status: 85% complete (data loaded, indexes building)
- **Infrastructure expansion** (2025-11-11): Dell-6330 laptop added to Proxmox cluster
  - Purpose: Temporary VM host to resolve Bot machine OOM issues
  - Specs: 8GB RAM, 256GB SSD
  - Will host VMs 101, 102, 103 (freeing ~8GB RAM on PVE host)
  - Enables Bot machine RAM reallocation (9.7GB → 16-18GB target)
- **NEW Q1-trained XGBoost model** (2025-11-10) with 3.5x better precision than old model
  - Model path: models/whipsaw_xgb_q1_2025/whipsaw_model_q1.json
  - Training: 128 samples from Q1 2025 (80/20 split)
  - Performance: 50% precision on Q1 test, 14.3% on Q2 validation
  - Blocks only 8% of trades vs old model's 56%
- Ground truth verification system for Q1/Q2 2025 (160 and 173 MACD crosses)
- Comprehensive model validation documentation (3 files, 21 KB):
  - MODEL_VALIDATION_FINDINGS_2025-11-10.md - Old model analysis (4-6% precision)
  - Q1_2025_MODEL_RETRAINING_2025-11-10.md - Complete retraining documentation
  - FINAL_MODEL_COMPARISON_2025-11-10.md - Head-to-head comparison showing 3.5x improvement
- Training scripts: train_q1_2025_from_ground_truth.py, validate_new_model_q2.py
- Verification script: verify_whipsaw_detection.py (generates ground truth CSVs)
- Q1 2025 validation script (test_q1_2025_validation.py, 535 lines) - untested due to infrastructure failure
- Q2 2025 walk-forward validation script (test_q2_2025_validation_optimized.py, 536 lines)
- Q2 2025 validation results (52 trades, CSV format)
- Comprehensive analysis documents (4 files, 73 KB total):
  - Position sizing analysis comparing 9 strategies
  - Profit factor improvement analysis (37.3% winner reversal finding)
  - Trailing stop results and configuration
  - Spirit architecture review
- Analysis scripts for threshold testing, profit factor improvements, position sizing
- Project backlog with 18 prioritized tasks
- Tomorrow's priorities planning document
- XGBoost whipsaw detection model (v3) with 11-year training dataset (2013-2025)
- ML experiment tracking framework with branch-based versioning
- Training pipeline (train_whipsaw_xgboost.py) with performance tracking
- Validation pipeline (validate_whipsaw_model.py) with backtest comparison
- ML experiment documentation (README, v1/v2/v3 version docs, template)
- Pre-entry feature engineering (histogram_strength, macd_divergence, price_vs_sma200)
- PostgreSQL whipsaw schema with 4 tables for MACD cross analysis
- Data pipeline for MACD cross detection, trade simulation, and ranking
- Technical indicator computation scripts with 31 indicators
- PostgreSQL database infrastructure (12 tables, 57 indexes, 3 schemas)
- CSV import pipeline for 21GB Kraken historical OHLC data
- Database utilities (backup, restore, monitoring, connection pooling)
- Migration framework with rollback support
- Monitoring daemon for import process
- Comprehensive security and credentials documentation

### Changed
- **Strategy direction** (2025-11-15): Fundamental shift from threshold-based to adaptive filtering
  - REJECTED "winning" filter config (SMA200 + ADX>20 + RSI<70)
  - Root cause: Blocks 88% of good trades (3/17 captured) with NEGATIVE avg P&L (-0.24%)
  - ADX-only filter superior: 88.2% capture rate (15/17) with positive avg P&L (+0.16%)
  - Paper trading deployment PAUSED pending filter redesign
  - New direction: Probabilistic/fuzzy logic + regime-adaptive filtering
- **Exit strategy priority** (2025-11-15): Exit improvement now higher priority than entry filtering
  - Analysis revealed 110.97% profit left on table (captures only 6.9% of potential)
  - Average potential: 7.86% per good trade
  - Average captured: 0.54% per good trade
  - Trailing stops moved to P0 priority
- **ML prediction API** (2025-11-14): Switched to native XGBoost DMatrix approach
  - Old: predict_proba() on scikit-learn wrapper (FAILED)
  - New: DMatrix + predict() on native Booster (SUCCESS)
  - Benefit: More reliable, better performance, avoids joblib issues
- **Feature extraction robustness** (2025-11-14): Added column name fallback logic
  - Checks both 'macd_hist' (Spirit) and 'macd_histogram' (training data)
  - Enhanced missing value detection with named tracking
  - Debug logging added for troubleshooting (10+ statements)
- **Branch status** (2025-11-14): feature/whipsaw-ml-v2 merged to develop
  - Merge commit: f90bd23
  - Status: ML strategy now in integration branch
  - Next: Paper trading validation
- **Infrastructure architecture** (2025-11-13): Hybrid cloud/local deployment
  - Bot compute workloads: Cloud (Hetzner CPX42, 16GB RAM)
  - PostgreSQL database: Cloud (Hetzner CX22, 8GB RAM)
  - Local Proxmox: 6 VMs operational (NodeRed, MQTT, InfluxDB, HomeAssistant, SQL01 backup)
  - Eliminated cluster complexity (single-node Proxmox, no Dell-6330)
  - Removed SharedPool NFS loopback mount (problematic design)
- **ML model status (2025-11-10):** NEW Q1-trained model ready for deployment
  - Old model: 4.1% precision, blocks 56% trades (UNACCEPTABLE)
  - New model: 14.3% precision, blocks 8% trades (READY FOR PAPER TRADING)
  - Improvement: 3.5x better precision, 7x fewer trades blocked
  - Next steps: Q2 P&L analysis, go/no-go decision for paper trading
- ML model v3 status: Superseded by Q1-trained model (old model had 11-year dataset but poor generalization)
- Production deployment: Unblocked - new model shows acceptable performance
- ML model feature set: removed lookahead bias features (MAE, MFE, bars_held) in v2
- ML model dataset: expanded from 3.5k to 28k trades (8.9x increase) in v3
- Feature importance shifted from ATR (v2, 56%) to SMA200 (v3, 27.8%) across market cycles
- Model selectivity: v3 allows 26.4% of trades vs v2's 15.5% (more balanced)

### Fixed
- **XGBoost prediction API error** (2025-11-14): ML guard integration failing
  - Root cause: Using predict_proba() on native Booster instead of DMatrix + predict()
  - Solution: Rewrote prediction logic to use XGBoost DMatrix API
  - Impact: ML guard now operational with 11 blocks at 0.7 threshold
- **Feature extraction bugs** (2025-11-14): Two bugs fixed in feature engineering
  - Bug 1: Column name mismatch (macd_hist vs macd_histogram)
  - Bug 2: Silent failures on missing values (no error reporting)
  - Solution: Added fallback logic and enhanced missing value tracking
  - Impact: All 9 features extracting correctly with comprehensive logging
- **RESOLVED (2025-11-13):** Bot machine OOM crisis after 3-day blockage
  - Solution: Migrated to Hetzner CPX42 with 16GB RAM (65% increase)
  - ML validation work now unblocked
  - Cloud provides stable, scalable resources vs local RAM constraints
- **RESOLVED (2025-11-13):** Proxmox cluster quorum issues
  - Removed Dell-6330 node from cluster
  - Single-node Proxmox cluster stable
- **RESOLVED (2025-11-13):** SharedPool NFS loopback mount issues
  - Disabled problematic NFS mount (PVE → PVE)
  - All VMs migrated to local-lvm direct storage
- **RESOLVED (2025-11-13):** VM 101 (NodeRed) boot failure
  - Restored from backup (2025-11-10)
  - All 6 local VMs now operational
- Lookahead bias in v1 ML model (v2 uses only pre-entry features)
- NumPy type conversion bug in technical indicators computation (psycopg2 compatibility)
- SQL01 VM stability after RAM overcommitment incident

### Issues Discovered
- **CRITICAL (2025-11-15):** SMA200 filter blocking good trades in trending markets
  - "Winning" config captures only 3/17 good trades (17.6%) with -0.24% avg P&L
  - SMA200 blocks 47% of good trades (8 out of 17)
  - Strategy over-optimized for choppy markets (Aug-Nov 2024), fails in trending markets (Q1 2025)
  - Threshold-based logic (ADX>20, RSI<70, price>SMA200) too rigid for market spectrum
  - Impact: Cannot deploy to paper trading with current filter configuration
  - Resolution: Shift to probabilistic/adaptive filtering approach
- **CRITICAL (2025-11-15):** Exit strategy leaving 110.97% profit on table
  - Good trades average +7.86% potential but only +0.54% captured (6.9% efficiency)
  - Exit inefficiency larger issue than entry filtering
  - Impact: Even perfect entry filter cannot overcome poor exits
  - Resolution: Implement trailing stops (P0 priority)
- **RESOLVED (2025-11-13):** Bot machine OOM events blocking ML validation work
  - Resolution: Cloud migration to Hetzner CPX42 (16GB RAM)
  - Status: Unblocked after 3-day blockage (2025-11-10, 11, 12)
- **CRITICAL (2025-11-11):** Bot machine OOM events blocking ML validation work (HISTORICAL)
  - 4 OOM killer events (Python scripts consuming 9.6GB on 9.7GB system)
  - Root cause: ML validation scripts loading large OHLC datasets with technical indicators
  - Scripts affected: test_q1_2025_validation.py, test_q2_2025_validation.py
  - Impact: All ML script execution blocked, Q2 baseline validation incomplete
  - Resolution: Infrastructure expansion (Dell-6330) to free RAM for Bot machine
  - Target: 16-18GB RAM allocation (sufficient for ML workloads)
- **CRITICAL (2025-11-07):** SQL01 VM data disk hardware failure (2TB USB SSD)
  - PostgreSQL I/O errors block all database access
  - VM 107 cannot boot after disk migration (fstab configuration mismatch)
  - USB storage reliability issues (multiple drives disconnected simultaneously)
  - No backup strategy for 21GB database (8,656 CSV files, 176M+ rows at risk)
  - 4TB drive (SSD2_4T) physically disconnected
- ML model v3 does not generalize to Q2 2025 data (distribution shift)
- Model too conservative: blocks 91.8% of Q2 2025 trades vs 73.6% on training data
- Insufficient trade volume: 52 trades in 3 months vs expected ~200+ trades
- Winner reversal problem: 37.3% of profitable trades reverse into losses (£33k opportunity cost)

### Performance
- ML training time: 1.44s for 28k samples (excellent scalability)
- ML inference time: <1ms per prediction
- Model profit factor: 1.17 (v3 filtered) vs 0.96 (baseline)
- Model win rate: 38.3% (v3 filtered) vs 32.4% (baseline)
- Total P&L improvement: +218.8% (+645% filtered vs -543% baseline on 28k trades)
- CSV import rate: 30,357 rows/second with bulk inserts

## [0.1.0] - 2025-10-31

### Added
- Initial PostgreSQL database deployment
- MACD cross strategy implementation
- Basic backtesting framework
- Environment-based strategy selection

---

## Version History

### Whipsaw ML Model Versions

| Version | Date | Branch | Status | Key Changes |
|---------|------|--------|--------|-------------|
| v1 | 2025-11-05 | feature/whipsaw-ml-v1 | Archived | Baseline with lookahead bias (reference only) |
| v2 | 2025-11-05 | feature/whipsaw-ml-v2 | Active | Fixed lookahead bias, 3.5k trades, ATR #1 feature |
| v3 | 2025-11-05 | feature/whipsaw-ml-v2 | Production | 28k trades (2013-2025), SMA200 #1 feature, +218.8% P&L |

### Database Schema Versions

| Version | Date | Description |
|---------|------|-------------|
| 001 | 2025-10-31 | Initial schema: public, dev, admin schemas with 12 tables |
| 002 | 2025-11-04 | Added whipsaw schema with 4 tables for ML analysis |

---

## Notes

- **Branch Strategy:** Feature branches merge to develop, then develop merges to main
- **ML Models:** Tracked via branch-based versioning (feature/whipsaw-ml-v{N})
- **Database Migrations:** Located in migrations/ with rollback scripts
- **Documentation:** Daily logs in docs/daily/, architectural context in docs/context.md

---

## Infrastructure Incidents

### 2025-11-13 - Cloud Migration Success (OOM Resolution)
**Severity:** High (resolved critical 3-day blockage)
**Status:** RESOLVED

**Timeline:**
- 06:00 - Morning planning, cloud migration decision approved
- 08:00 - Hetzner account created, Bot server (CPX42) provisioned
- 10:00 - PostgreSQL server (CX22) provisioned, PostgreSQL 16 installed
- 12:00 - Database dump started on local SQL01 (64GB → 5.0GB compressed)
- 13:30 - Database transfer to cloud (3.5 minutes, 23 MB/s)
- 14:00 - Database restore started (pg_restore --jobs=4)
- 15:00 - Bot code deployed to cloud server
- 16:00 - Proxmox repairs: removed Dell-6330, disabled SharedPool NFS
- 17:00 - VM 101 restored from backup
- 18:00 - Database restore 85% complete (data loaded, indexes building overnight)

**Actions:**
- Migrated Bot server to Hetzner Cloud CPX42 (16GB RAM, 8 vCPU, 188.245.209.204)
- Created dedicated PostgreSQL server on Hetzner CX22 (8GB RAM, 2 vCPU, 188.245.98.89)
- Migrated 64GB trading_bot database (92% compression, parallel restore)
- Fixed Proxmox cluster (removed Dell-6330 node, single-node cluster stable)
- Removed SharedPool NFS loopback mount (problematic design)
- Restored VM 101 (NodeRed) from backup after migration failure

**Impact:**
- OOM crisis RESOLVED after 3-day blockage
- ML validation work UNBLOCKED (can resume Q2 baseline validation tomorrow)
- Infrastructure complexity REDUCED (6 VMs local, no cluster, no NFS)
- Professional hosting ENABLED (99.9% uptime, backup solutions, monitoring)

**Cost Analysis:**
- Cloud: €28.70/month (~£25/month)
- Opportunity cost saved: £180-240 (3 days troubleshooting)
- Break-even: Already exceeded 18-24 months of cloud costs

**Lessons Learned:**
- Cloud economics compelling when troubleshooting exceeds 2 hours
- Database compression critical: 92% reduction enabled fast migration
- Separation of concerns: Dedicated database server better than colocated
- Parallel restore matters: 4 workers cut restore time in half
- Keep rollback options: Local SQL01 running during migration provides safety net
- Infrastructure complexity has hidden costs: Simple local setup more reliable

**Reference:** docs/daily/2025-11-13.md

---

### 2025-11-10 to 2025-11-12 - Bot Machine OOM Crisis (3 Days)
**Severity:** Critical (blocked all ML work)
**Status:** RESOLVED (2025-11-13 via cloud migration)

**Timeline:**
- 2025-11-10: OOM discovered during Q2 validation (Day 1)
- 2025-11-11: Dell-6330 provisioned as cluster host (Day 2)
- 2025-11-12: VM migration failed, cloud research (Day 3)
- 2025-11-13: Cloud migration executed, OOM resolved

**Root Cause:**
- Bot machine (192.168.1.30) with 9.7GB RAM insufficient for ML workloads
- ML validation scripts loading large OHLC datasets (9.6GB peak memory)
- 4 OOM killer events blocked all ML script execution

**Impact:**
- 3 days of zero ML progress (2025-11-10, 11, 12)
- Q2 2025 baseline validation incomplete
- ML priorities from 2025-11-10 blocked
- 9-12 hours consumed in local infrastructure troubleshooting

**Resolution:**
- Cloud migration to Hetzner CPX42 (16GB RAM, 65% increase)
- Professional hosting eliminates local hardware constraints
- Scalable resources for future ML workloads

**Reference:** docs/daily/2025-11-10.md, 2025-11-11.md, 2025-11-12.md, 2025-11-13.md

---

### 2025-11-07 - SQL01 Storage Failure
**Severity:** Critical
**Status:** Partially Resolved (VM offline, requires fstab repair)

**Timeline:**
- 09:00 - Attempted Q1 2025 validation, PostgreSQL I/O errors discovered
- 10:00 - Root cause identified: 2TB USB SSD (SSD01_2T) hardware failure
- 11:00 - Emergency data migration from 2TB to 500GB backup drive
- 13:00 - USB power cycle restored 2TB drive (health uncertain)
- 14:00 - VM 107 boot failure discovered (fstab mismatch with new disk)
- 17:00 - Day ended with VM offline, Monday recovery planned

**Impact:**
- All ML validation work blocked (no database access)
- Zero code progress on 2025-11-07
- Q1 2025 validation script created but untested
- Distribution shift investigation blocked

**Resolution Plan (Monday 2025-11-10):**
1. Boot VM 107 in rescue mode
2. Update fstab with new disk UUIDs
3. Verify database integrity (176M+ rows)
4. Reconnect 4TB drive (SSD2_4T)
5. Implement emergency backup strategy
6. Assess 2TB drive health (SMART diagnostics)

**Lessons Learned:**
- USB storage unreliable for production databases
- No backup strategy = unacceptable risk
- Need hardware health monitoring (SMART, I/O errors)
- Document disaster recovery procedures

### 2025-11-03 - SQL01 VM Crash
**Severity:** High
**Status:** Resolved

**Cause:** Proxmox RAM overcommitment during CSV import
**Resolution:** Within 1 hour, zero data loss
**Reference:** docs/SQL01_INCIDENT_RESOLUTION.md

---

**Last Updated:** 2025-11-13
