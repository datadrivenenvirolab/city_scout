# City Scout

<p align="center">
  <b>Find, download, and classify Climate Action Plans (CAPs) for Cities and Regional Governments - automatically.</b>
</p>

---

## 🌍 What is City Scout?

**City Scout** is a high-performance Python pipeline designed to automate the discovery and analysis of Climate Action Plans and related sustainability strategies for municipalities and regional entities worldwide.

It leverages [**ScaleSERP**](https://trajectdata.com/serp/scale-serp-api/) for targeted document discovery and [**OpenAI**](https://openai.com/api/) (GPT-5-mini) for precise document classification, ensuring that only relevant, high-quality climate reports are preserved.

---

## ✨ Features

- 🌐 **Multilingual Discovery**: Intelligent search using local Google domains to find documents in native languages with English fallbacks.
- 🔎 **Two-Stage Pipeline**:
    - **Stage 1**: Discovery and heuristic scoring of potential PDF and page links.
    - **Stage 2**: Robust downloading, text extraction, and AI-powered classification.
- 🧠 **Context-Aware Classification**: Validates relevance, matching entity (City/Region), report type, and publication year.
- 🧹 **Clean Artifacts**: Automatically organizes outputs into validated PDFs, rich CSV summaries, and audit logs for failed or mismatched items.
- ⚙️ **Highly Configurable**: Deep control over search parameters, acceptable document types, and year gates via `config.py`.

---

## How it Works

![Code logic diagram](code_logic_graph.png)

### Stage 1: Discovery
For each target (City/Region), the pipeline performs optimized searches for direct PDF links and landing pages. Results are scored based on domain relevance and keyword matches (e.g., "Climate Action Plan", "Sustainability Strategy").

### Stage 2: Processing & Guardrails
Documents are downloaded and the first several pages are analyzed by an LLM to confirm:
1. **Relevance**: Is this a Climate Action or related Sustainability plan?
2. **Entity Match**: Does it belong to the specific city or region?
3. **Type & Year**: Does it meet the minimum year requirement and allowed document categories?

---

## 🚀 Setup & Installation

### 1. Prerequisites
- Python 3.10+
- OpenAI API Key
- ScaleSERP API Key

### 2. Isolation (Virtual Environment)
```bash
# Create the venv
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate

# Activate (Unix/macOS)
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment
Create a `.env` file in the root directory (refer to `env.example`):
```env
OPENAI_API_KEY=your_key_here
SCALESERP_API_KEY=your_key_here
```

---

## 🛠 Usage

### 1. Prepare Input
Create a CSV file following the `cities_csv_template.csv`. Required columns typically include `City` and `Country`.

### 2. Run the Pipeline
```bash
python run_pipeline.py --input-csv your_cities.csv -v
```
- `-v`: Shows progress and basic info.
- `-vv`: Enables full debug logging.

### 3. Explore Outputs
All results are stored in the `outputs/` directory (timestamped or job-named):
- `pdfs/`: Validated climate reports.
- `stage2_results.csv`: Summary of all successfully processed targets.
- `search_cache.json`: Cached search results to minimize API costs during re-runs.

---

## 🔧 Configuration

Core settings are managed in `report_fetcher/config.py`. Key parameters include:
- `ACCEPTABLE_REPORT_TYPES`: List of document labels to accept (e.g., "Net Zero Plan", "GHG Inventory").
- `MIN_ACCEPTABLE_REPORT_YEAR`: Filter out older documents.
- `MAX_PDF_RESULTS`: Number of top results to attempt per city.

---

## 🤝 Contributing & Support

- **Bugs/Features**: Please open an issue with detailed reproduction steps.