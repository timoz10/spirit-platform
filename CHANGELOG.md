# Changelog

All notable changes to the kraken-bot project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
- ML model feature set: removed lookahead bias features (MAE, MFE, bars_held) in v2
- ML model dataset: expanded from 3.5k to 28k trades (8.9x increase) in v3
- Feature importance shifted from ATR (v2, 56%) to SMA200 (v3, 27.8%) across market cycles
- Model selectivity: v3 allows 26.4% of trades vs v2's 15.5% (more balanced)

### Fixed
- Lookahead bias in v1 ML model (v2 uses only pre-entry features)
- NumPy type conversion bug in technical indicators computation (psycopg2 compatibility)
- SQL01 VM stability after RAM overcommitment incident

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

**Last Updated:** 2025-11-05
