# Weather Station

A small weather website: search a city, see current conditions, the next 24
hours as a temperature trace, and a five day outlook. Data comes from
[Open-Meteo](https://open-meteo.com), which is free and needs no API key.

## Run it

```
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000 in a browser.

## Files

- `app.py` — the server: fetches data, shapes it, hands it to the template
- `templates/index.html` — the page markup
- `static/style.css` — the styling

## Things to try next

- Cache responses so repeated searches don't re-hit the API
- Add a "use my location" button with the browser's geolocation API
- Save recent searches to a small SQLite database
- Deploy it to Render, Fly.io, or PythonAnywhere
