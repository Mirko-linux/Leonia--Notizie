import feedparser
import requests
import time
import sqlite3
import logging
from newspaper import Article
from datetime import datetime
import nltk
import os


# === DOWNLOAD NLTK DATA ===
try:
    nltk.download('punkt', quiet=True)
    nltk.download('punkt_tab', quiet=True)
except:
    pass


BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

RSS_FEEDS = [
    'https://www.ansa.it/sito/ansait_rss.xml',
    'https://www.repubblica.it/rss/homepage/rss2.0.xml',
    'https://www.ilpost.it/feed/'
]
# Configura logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Database per link persistenti
def init_db():
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS posted_links
                 (link TEXT PRIMARY KEY, published_at TIMESTAMP)''')
    conn.commit()
    conn.close()

def is_link_posted(link):
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()
    c.execute("SELECT 1 FROM posted_links WHERE link = ?", (link,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_link_posted(link):
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()
    c.execute("INSERT INTO posted_links (link, published_at) VALUES (?, ?)", 
              (link, datetime.now()))
    conn.commit()
    conn.close()

# === FUNZIONE PER ESTRARRE IMMAGINE E TESTO ===
def estrai_dati(link):
    try:
        # Configura Article per essere più robusto
        config = {
            'request_timeout': 10,
            'follow_meta_refresh': True,
            'browser_user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        articolo = Article(link, **config)
        articolo.download()
        articolo.parse()
        
        # Usa NLP solo se disponibile, altrimenti usa testo normale
        try:
            articolo.nlp()
            summary = articolo.summary
        except:
            summary = articolo.text[:200] + "..." if len(articolo.text) > 200 else articolo.text
            
        return articolo.top_image, articolo.title, summary, articolo.text
    except Exception as e:
        logging.error(f"Errore nell'estrazione da {link}: {e}")
        return None, None, None, None

# === ANALISI SEMPLICE ===
def analizza_articolo(titolo, testo, entry):
    try:
        # Prova prima con il testo estratto, poi con il summary del feed
        if testo and len(testo) > 50:
            frasi = testo.split('. ')
            intro = '. '.join(frasi[:2]) if len(frasi) >= 2 else testo[:300] + "..."
            return f"📰 {titolo}\n\n{intro.strip()}"
        elif entry.get('summary'):
            # Usa il summary dal feed RSS
            summary = entry.summary
            # Rimuovi tag HTML
            import re
            summary = re.sub('<[^<]+?>', '', summary)
            return f"📰 {titolo}\n\n{summary[:300]}..."
        else:
            return f"📰 {titolo}\n\nLeggi l'articolo completo per i dettagli."
    except Exception as e:
        logging.error(f"Errore nell'analisi: {e}")
        return f"📰 {titolo}"

# === PUBBLICAZIONE SU TELEGRAM ===
def pubblica_su_telegram(titolo, link, immagine, analisi):
    try:
        messaggio = f"{analisi}\n\n🔗 {link}"
        
        # Pulisci il messaggio per Telegram
        messaggio = messaggio.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        
        if immagine and immagine.startswith('http'):
            try:
                # Prova a inviare con immagine
                img_response = requests.get(immagine, timeout=10)
                if img_response.status_code == 200:
                    response = requests.post(
                        f'https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto',
                        data={
                            'chat_id': CHAT_ID,
                            'caption': messaggio[:1024],
                            'parse_mode': 'HTML'
                        },
                        files={'photo': img_response.content}
                    )
                else:
                    raise Exception("Immagine non disponibile")
            except:
                # Fallback a messaggio senza immagine
                response = requests.post(
                    f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
                    json={
                        'chat_id': CHAT_ID,
                        'text': messaggio,
                        'parse_mode': 'HTML'
                    }
                )
        else:
            response = requests.post(
                f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
                json={
                    'chat_id': CHAT_ID,
                    'text': messaggio,
                    'parse_mode': 'HTML'
                }
            )
        
        if response.status_code == 200:
            logging.info(f"Articolo pubblicato: {titolo}")
            return True
        else:
            logging.error(f"Errore Telegram: {response.text}")
            return False
            
    except Exception as e:
        logging.error(f"Errore nell'invio a Telegram: {e}")
        return False

# === CICLO PRINCIPALE ===
def fetch_and_send():
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            logging.info(f"Controllando feed: {url} - {len(feed.entries)} articoli")
            
            for entry in feed.entries[:5]:  # Solo ultimi 5 articoli
                link = entry.link
                
                if not is_link_posted(link):
                    logging.info(f"Nuovo articolo trovato: {link}")
                    
                    immagine, titolo, riassunto, testo = estrai_dati(link)
                    
                    # Se l'estrazione fallisce, usa i dati dal feed
                    if not titolo and hasattr(entry, 'title'):
                        titolo = entry.title
                    
                    if titolo:
                        analisi = analizza_articolo(titolo, testo or riassunto, entry)
                        if pubblica_su_telegram(titolo, link, immagine, analisi):
                            mark_link_posted(link)
                            time.sleep(3)  # Pausa tra messaggi per evitare rate limit
                    
        except Exception as e:
            logging.error(f"Errore nel processare feed {url}: {e}")

# === AVVIO ===
if __name__ == "__main__":
    # Scarica tutti i dati NLTK necessari
    try:
        nltk.download('punkt', quiet=True)
        nltk.download('punkt_tab', quiet=True)
        nltk.download('averaged_perceptron_tagger', quiet=True)
    except Exception as e:
        logging.warning(f"Alcuni download NLTK falliti: {e}")
    
    init_db()
    logging.info("Bot avviato...")
    
    while True:
        try:
            fetch_and_send()
            time.sleep(60)  # Controlla ogni minuto
        except KeyboardInterrupt:
            logging.info("Bot fermato dall'utente")
            break
        except Exception as e:
            logging.error(f"Errore nel loop principale: {e}")
            time.sleep(60)
