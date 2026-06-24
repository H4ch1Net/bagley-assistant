# Bagley Assistant

A Python-based personal assistant built to automate tasks, answer questions, and make your workflow a little less painful.

## Features

- Conversational interface powered by an LLM backend
- Modular command system for extending functionality
- Lightweight and easy to run locally
- Configurable via environment variables

## Requirements

- Python 3.10+
- A `.env` file with your API keys (see `.env.example`)

## Getting Started

```bash
git clone https://github.com/H4ch1Net/bagley-assistant.git
cd bagley-assistant
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

Then run:

```bash
python main.py
```

## Configuration

All configuration is handled through environment variables in your `.env` file. Check `.env.example` for available options.

## Contributing

Pull requests are welcome. For major changes, open an issue first to discuss what you'd like to change.

## License

[MIT](LICENSE)
