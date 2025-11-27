import feedparser
import requests
import time
import sqlite3
import logging
from newspaper import Article
from datetime import datetime
import nltk
import os
import re


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

# Database per link persistenti e digest pubblicati
def init_db():
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS posted_links
                 (link TEXT PRIMARY KEY, published_at TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS posted_digests
                 (digest_time TIMESTAMP PRIMARY KEY)''')
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
    c.execute("INSERT OR IGNORE INTO posted_links (link, published_at) VALUES (?, ?)", 
              (link, datetime.now()))
    conn.commit()
    conn.close()

def is_digest_sent_this_hour():
    """Controlla se è già stato inviato un digest in quest'ora"""
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()
    current_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    c.execute("SELECT 1 FROM posted_digests WHERE digest_time = ?", (current_hour,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_digest_sent():
    """Segna che il digest di quest'ora è stato inviato"""
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()
    current_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    c.execute("INSERT OR IGNORE INTO posted_digests (digest_time) VALUES (?)", (current_hour,))
    conn.commit()
    conn.close()

# === FUNZIONE PER ESTRARRE DATI ===
def estrai_dati(link):
    try:
        config = {
            'request_timeout': 10,
            'follow_meta_refresh': True,
            'browser_user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        articolo = Article(link, **config)
        articolo.download()
        articolo.parse()
        
        try:
            articolo.nlp()
            summary = articolo.summary
        except:
            summary = articolo.text[:200] + "..." if len(articolo.text) > 200 else articolo.text
            
        return articolo.top_image, articolo.title, summary, articolo.text
    except Exception as e:
        logging.error(f"Errore nell'estrazione da {link}: {e}")
        return None, None, None, None

# === ANALISI E RIASSUNTO BREVE ===
def crea_riassunto_breve(titolo, testo, entry):
    """Crea un riassunto breve per il digest"""
    try:
        if testo and len(testo) > 50:
            frasi = testo.split('. ')
            intro = frasi[0] if frasi else testo[:150]
            return intro.strip()
        elif entry.get('summary'):
            summary = re.sub('<[^<]+?>', '', entry.summary)
            return summary[:150].strip()
        else:
            return "Dettagli nell'articolo completo."
    except Exception as e:
        logging.error(f"Errore nel riassunto: {e}")
        return "Leggi l'articolo per i dettagli."

# === FILTRA NOTIZIE SIMILI ===
def sono_simili(titolo1, titolo2):
    """Controlla se due titoli sono troppo simili (stesso argomento)"""
    # Normalizza i titoli
    t1 = titolo1.lower().strip()
    t2 = titolo2.lower().strip()
    
    # Se sono identici
    if t1 == t2:
        return True
    
    # Estrai parole chiave (rimuovi parole comuni)
    stop_words = {'il', 'lo', 'la', 'i', 'gli', 'le', 'un', 'una', 'di', 'da', 'in', 'per', 'con', 'su', 'a', 'è', 'e', 'che', 'del', 'della', 'dei'}
    parole1 = set([p for p in t1.split() if len(p) > 3 and p not in stop_words])
    parole2 = set([p for p in t2.split() if len(p) > 3 and p not in stop_words])
    
    # Se più del 60% delle parole sono in comune
    if len(parole1) > 0 and len(parole2) > 0:
        comuni = len(parole1.intersection(parole2))
        percentuale = comuni / min(len(parole1), len(parole2))
        return percentuale > 0.6
    
    return False

def filtra_notizie_duplicate(notizie):
    """Rimuove notizie duplicate o troppo simili"""
    notizie_filtrate = []
    
    for notizia in notizie:
        duplicata = False
        for n_filtrata in notizie_filtrate:
            if sono_simili(notizia['titolo'], n_filtrata['titolo']):
                duplicata = True
                break
        
        if not duplicata:
            notizie_filtrate.append(notizia)
    
    return notizie_filtrate

# === RACCOLTA NOTIZIE DELL'ORA ===
def raccogli_notizie_nuove():
    """Raccoglie fino a 5 notizie nuove da tutti i feed"""
    notizie = []
    
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            logging.info(f"Controllando feed: {url} - {len(feed.entries)} articoli")
            
            for entry in feed.entries[:10]:  # Controlla i primi 10
                link = entry.link
                
                if not is_link_posted(link) and len(notizie) < 10:  # Raccogliamo più notizie per poi filtrare
                    immagine, titolo, riassunto, testo = estrai_dati(link)
                    
                    if not titolo and hasattr(entry, 'title'):
                        titolo = entry.title
                    
                    if titolo:
                        riassunto_breve = crea_riassunto_breve(titolo, testo or riassunto, entry)
                        notizie.append({
                            'titolo': titolo,
                            'link': link,
                            'riassunto': riassunto_breve,
                            'immagine': immagine
                        })
                        mark_link_posted(link)  # Segna subito come letto
                    
        except Exception as e:
            logging.error(f"Errore nel processare feed {url}: {e}")
    
    # Filtra duplicati
    notizie = filtra_notizie_duplicate(notizie)
    
    # Limita a 5 notizie
    return notizie[:5]

# === CREA E INVIA DIGEST ===
def crea_e_invia_digest(notizie):
    """Crea un singolo post con tutte le notizie"""
    if not notizie:
        logging.info("Nessuna notizia nuova per il digest")
        return False
    
    # Intestazione con ora
    ora_attuale = datetime.now().strftime("%H:%M")
    messaggio = f"📰 <b>NOTIZIARIO - Ore {ora_attuale}</b>\n\n"
    
    # Aggiungi ogni notizia
    for i, notizia in enumerate(notizie, 1):
        messaggio += f"<b>{i}. {notizia['titolo']}</b>\n"
        messaggio += f"{notizia['riassunto']}\n"
        messaggio += f"🔗 {notizia['link']}\n\n"
    
    # Invia a Telegram
    try:
        response = requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json={
                'chat_id': CHAT_ID,
                'text': messaggio,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True
            }
        )
        
        if response.status_code == 200:
            logging.info(f"Digest pubblicato con {len(notizie)} notizie")
            mark_digest_sent()
            return True
        else:
            logging.error(f"Errore Telegram: {response.text}")
            return False
            
    except Exception as e:
        logging.error(f"Errore nell'invio a Telegram: {e}")
        return False

# === VERIFICA ORARIO ===
def is_orario_attivo():
    """Verifica se siamo nell'orario di pubblicazione (6:00 - 21:00)"""
    ora_corrente = datetime.now().hour
    return 6 <= ora_corrente < 21

# === CICLO PRINCIPALE ===
def main_loop():
    while True:
        try:
            ora_attuale = datetime.now()
            
            # Verifica se siamo nell'orario attivo
            if not is_orario_attivo():
                logging.info("Fuori dall'orario di pubblicazione (6:00-21:00). In attesa...")
                time.sleep(300)  # Controlla ogni 5 minuti di notte
                continue
            
            # Verifica se abbiamo già pubblicato in quest'ora
            if is_digest_sent_this_hour():
                minuti_alla_prossima_ora = 60 - ora_attuale.minute
                logging.info(f"Digest già inviato per quest'ora. Prossimo tra {minuti_alla_prossima_ora} minuti...")
                time.sleep(300)  # Controlla ogni 5 minuti
                continue
            
            # È una nuova ora, raccogliamo e inviamo le notizie
            logging.info("Raccogliendo notizie per il digest orario...")
            notizie = raccogli_notizie_nuove()
            
            if notizie:
                crea_e_invia_digest(notizie)
            else:
                logging.info("Nessuna notizia nuova trovata")
                mark_digest_sent()  # Segna comunque per evitare tentativi ripetuti
            
            # Attendi prima del prossimo controllo
            time.sleep(300)  # Controlla ogni 5 minuti
            
        except KeyboardInterrupt:
            logging.info("Bot fermato dall'utente")
            break
        except Exception as e:
            logging.error(f"Errore nel loop principale: {e}")
            time.sleep(300)

# === AVVIO ===
if __name__ == "__main__":
    try:
        nltk.download('punkt', quiet=True)
        nltk.download('punkt_tab', quiet=True)
        nltk.download('averaged_perceptron_tagger', quiet=True)
    except Exception as e:
        logging.warning(f"Alcuni download NLTK falliti: {e}")
    
    init_db()
    logging.info("Bot notiziario avviato...")
    logging.info("Pubblicherà digest orari dalle 6:00 alle 21:00")
    
    main_loop()
