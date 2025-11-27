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
import google.generativeai as genai


# === DOWNLOAD NLTK DATA ===
try:
    nltk.download('punkt', quiet=True)
    nltk.download('punkt_tab', quiet=True)
except:
    pass


BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Configura Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash-exp')

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
            summary = articolo.text[:500] + "..." if len(articolo.text) > 500 else articolo.text
            
        return articolo.top_image, articolo.title, summary, articolo.text
    except Exception as e:
        logging.error(f"Errore nell'estrazione da {link}: {e}")
        return None, None, None, None

# === ESTRAI CONTENUTO BASE ===
def estrai_contenuto_base(entry):
    """Estrae contenuto base dal feed entry"""
    titolo = entry.title if hasattr(entry, 'title') else "Notizia senza titolo"
    
    contenuto = ""
    if hasattr(entry, 'summary'):
        contenuto = re.sub('<[^<]+?>', '', entry.summary)
    elif hasattr(entry, 'description'):
        contenuto = re.sub('<[^<]+?>', '', entry.description)
    
    return titolo, contenuto[:300]

# === RACCOLTA NOTIZIE DELL'ORA ===
def raccogli_notizie_nuove():
    """Raccoglie notizie nuove da tutti i feed"""
    notizie = []
    
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            logging.info(f"Controllando feed: {url} - {len(feed.entries)} articoli")
            
            for entry in feed.entries[:15]:  # Controlla i primi 15
                link = entry.link
                
                if not is_link_posted(link) and len(notizie) < 12:
                    # Prova prima con newspaper
                    immagine, titolo, riassunto, testo = estrai_dati(link)
                    
                    # Fallback ai dati del feed
                    if not titolo:
                        titolo, contenuto_base = estrai_contenuto_base(entry)
                        testo = contenuto_base
                    
                    if titolo:
                        notizie.append({
                            'titolo': titolo,
                            'link': link,
                            'contenuto': testo or riassunto or "Contenuto non disponibile",
                            'immagine': immagine
                        })
                        mark_link_posted(link)
                    
        except Exception as e:
            logging.error(f"Errore nel processare feed {url}: {e}")
    
    return notizie

# === ANALISI CON GEMINI ===
def analizza_con_gemini(notizie):
    """Usa Gemini per analizzare e organizzare le notizie"""
    if not notizie:
        return None
    
    try:
        # Prepara il testo per Gemini
        testo_notizie = ""
        for i, notizia in enumerate(notizie, 1):
            testo_notizie += f"\n--- NOTIZIA {i} ---\n"
            testo_notizie += f"Titolo: {notizia['titolo']}\n"
            testo_notizie += f"Contenuto: {notizia['contenuto'][:400]}\n"
            testo_notizie += f"Link: {notizia['link']}\n"
        
        prompt = f"""Sei un giornalista professionista che cura un notiziario orario in italiano.

Analizza queste notizie e:
1. Elimina le notizie duplicate o molto simili (stesso argomento/evento)
2. Seleziona le 5 notizie più importanti e interessanti
3. Organizzale per rilevanza (più importante prima)
4. Per ogni notizia selezionata, scrivi un riassunto chiaro di 2-3 righe in italiano

NOTIZIE DA ANALIZZARE:
{testo_notizie}

Rispondi SOLO con questo formato (niente altro testo):

NOTIZIA 1
Titolo: [titolo originale]
Riassunto: [il tuo riassunto in 2-3 righe]
Link: [link originale]

NOTIZIA 2
Titolo: [titolo originale]
Riassunto: [il tuo riassunto in 2-3 righe]
Link: [link originale]

... (fino a massimo 5 notizie)
"""
        
        logging.info("Invio richiesta a Gemini...")
        response = model.generate_content(prompt)
        
        if response and response.text:
            logging.info("Risposta ricevuta da Gemini")
            return response.text
        else:
            logging.error("Gemini non ha restituito una risposta valida")
            return None
            
    except Exception as e:
        logging.error(f"Errore nell'analisi con Gemini: {e}")
        return None

# === PARSING RISPOSTA GEMINI ===
def parse_risposta_gemini(risposta_gemini):
    """Estrae le notizie dalla risposta di Gemini"""
    notizie_organizzate = []
    
    try:
        # Dividi per notizie
        blocchi = re.split(r'NOTIZIA \d+', risposta_gemini)
        
        for blocco in blocchi[1:]:  # Salta il primo elemento vuoto
            titolo_match = re.search(r'Titolo:\s*(.+?)(?:\n|$)', blocco)
            riassunto_match = re.search(r'Riassunto:\s*(.+?)(?=Link:|$)', blocco, re.DOTALL)
            link_match = re.search(r'Link:\s*(.+?)(?:\n|$)', blocco)
            
            if titolo_match and riassunto_match and link_match:
                notizie_organizzate.append({
                    'titolo': titolo_match.group(1).strip(),
                    'riassunto': riassunto_match.group(1).strip(),
                    'link': link_match.group(1).strip()
                })
        
        return notizie_organizzate[:5]  # Massimo 5 notizie
        
    except Exception as e:
        logging.error(f"Errore nel parsing della risposta Gemini: {e}")
        return []

# === CREA E INVIA DIGEST ===
def crea_e_invia_digest(notizie_analizzate):
    """Crea un singolo post con le notizie analizzate da Gemini"""
    if not notizie_analizzate:
        logging.info("Nessuna notizia da pubblicare")
        return False
    
    # Intestazione con ora
    ora_attuale = datetime.now().strftime("%H:%M")
    messaggio = f"📰 <b>NOTIZIARIO LEONIA+ - Ore {ora_attuale}</b>\n"
    messaggio += f"<i>Analizzato da Gemini AI</i>\n\n"
    
    # Aggiungi ogni notizia
    for i, notizia in enumerate(notizie_analizzate, 1):
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
            logging.info(f"Digest pubblicato con {len(notizie_analizzate)} notizie")
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
            
            # È una nuova ora, raccogliamo e analizziamo le notizie
            logging.info("Raccogliendo notizie per il digest orario...")
            notizie = raccogli_notizie_nuove()
            
            if notizie:
                logging.info(f"Raccolte {len(notizie)} notizie, invio a Gemini per analisi...")
                risposta_gemini = analizza_con_gemini(notizie)
                
                if risposta_gemini:
                    notizie_organizzate = parse_risposta_gemini(risposta_gemini)
                    if notizie_organizzate:
                        crea_e_invia_digest(notizie_organizzate)
                    else:
                        logging.error("Parsing risposta Gemini fallito")
                        mark_digest_sent()
                else:
                    logging.error("Gemini non ha risposto, salto questo digest")
                    mark_digest_sent()
            else:
                logging.info("Nessuna notizia nuova trovata")
                mark_digest_sent()
            
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
    logging.info("Bot notiziario avviato con Gemini AI...")
    logging.info("Pubblicherà digest orari dalle 6:00 alle 21:00")
    
    main_loop()
