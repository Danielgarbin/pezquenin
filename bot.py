import discord
import psycopg2
import psycopg2.extras
from discord.ext import commands
import json
import random
import os
import re
import threading
import unicodedata
import asyncio
import dateparser
import datetime
from flask import Flask, request, jsonify

######################################
# CONFIGURACIÓN: IDs y Servidor
######################################
OWNER_ID = 1336609089656197171         # Tu Discord ID (único autorizado para comandos sensibles)
PRIVATE_CHANNEL_ID = 1338130641354620988  # Canal privado para comandos sensibles (no se utiliza en la versión final)
PUBLIC_CHANNEL_ID  = 1338126297666424874  # Canal público donde se muestran resultados sensibles
SPECIAL_HELP_CHANNEL = 1337708244327596123  # Canal especial para que el owner reciba la lista extendida de comandos
GUILD_ID = 123456789012345678            # REEMPLAZA con el ID real de tu servidor (guild)

API_SECRET = os.environ.get("API_SECRET")  # Para la API privada (opcional)

######################################
# CONEXIÓN A LA BASE DE DATOS POSTGRESQL
######################################
DATABASE_URL = os.environ.get("DATABASE_URL")  # Usualmente la Internal Database URL de Render
conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True

def init_db():
    with conn.cursor() as cur:
        # Tabla de participantes
        cur.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                id TEXT PRIMARY KEY,
                nombre TEXT,
                puntos INTEGER DEFAULT 0,
                symbolic INTEGER DEFAULT 0,
                etapa INTEGER DEFAULT 1,
                logros JSONB DEFAULT '[]'
            )
        """)
        # Tabla de chistes
        cur.execute("""
            CREATE TABLE IF NOT EXISTS jokes (
                id SERIAL PRIMARY KEY,
                joke_text TEXT NOT NULL
            )
        """)
        # Tabla de trivias
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trivia (
                id SERIAL PRIMARY KEY,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                hint TEXT NOT NULL
            )
        """)
        # Tabla de notificaciones programadas
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                scheduled_time TIMESTAMP NOT NULL,
                recipients TEXT NOT NULL,
                message TEXT NOT NULL
            )
        """)
        # Tabla de eventos del calendario
        cur.execute("""
            CREATE TABLE IF NOT EXISTS calendar_events (
                id SERIAL PRIMARY KEY,
                event_time TIMESTAMP NOT NULL,
                description TEXT NOT NULL
            )
        """)
init_db()

######################################
# CONFIGURACIÓN INICIAL DEL TORNEO
######################################
PREFIX = '!'
# Configuración de etapas: cada etapa tiene un número determinado de jugadores.
STAGES = {1: 60, 2: 48, 3: 32, 4: 24, 5: 14}
current_stage = 1
stage_names = {
    1: "Battle Royale",
    2: "Snipers vs Runners",
    3: "Boxfight duos",
    4: "Pescadito dice",
    5: "Gran Final"
}

######################################
# VARIABLE GLOBAL PARA TRIVIA
######################################
active_trivia = {}  # key: channel.id, value: {"question": ..., "answer": ..., "hint": ...}

######################################
# FUNCIONES PARA LA BASE DE DATOS
######################################
def get_participant(user_id):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM participants WHERE id = %s", (user_id,))
        return cur.fetchone()

def get_all_participants():
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM participants")
        rows = cur.fetchall()
        data = {"participants": {}}
        for row in rows:
            data["participants"][row["id"]] = row
        return data

def upsert_participant(user_id, participant):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO participants (id, nombre, puntos, symbolic, etapa, logros)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                nombre = EXCLUDED.nombre,
                puntos = EXCLUDED.puntos,
                symbolic = EXCLUDED.symbolic,
                etapa = EXCLUDED.etapa,
                logros = EXCLUDED.logros
        """, (
            user_id,
            participant["nombre"],
            participant.get("puntos", 0),
            participant.get("symbolic", 0),
            participant.get("etapa", current_stage),
            json.dumps(participant.get("logros", []))
        ))

def update_score(user: discord.Member, delta: int):
    user_id = str(user.id)
    participant = get_participant(user_id)
    if participant is None:
        participant = {
            "nombre": user.display_name,
            "puntos": 0,
            "symbolic": 0,
            "etapa": current_stage,
            "logros": []
        }
    new_points = int(participant.get("puntos", 0)) + delta
    participant["puntos"] = new_points
    upsert_participant(user_id, participant)
    return new_points

def award_symbolic_reward(user: discord.Member, reward: int):
    user_id = str(user.id)
    participant = get_participant(user_id)
    if participant is None:
        participant = {
            "nombre": user.display_name,
            "puntos": 0,
            "symbolic": 0,
            "etapa": current_stage,
            "logros": []
        }
    current_symbolic = int(participant.get("symbolic", 0))
    new_symbolic = current_symbolic + reward
    participant["symbolic"] = new_symbolic
    upsert_participant(user_id, participant)
    return new_symbolic

######################################
# NORMALIZACIÓN DE CADENAS
######################################
def normalize_string(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', s)
                   if not unicodedata.combining(c)).replace(" ", "").lower()

######################################
# FUNCIONES PARA CHISTES Y TRIVIAS
######################################
def get_random_joke():
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM jokes")
        joke_ids = [row[0] for row in cur.fetchall()]
    if not joke_ids:
        return "No hay chistes disponibles."
    joke_id = random.choice(joke_ids)
    with conn.cursor() as cur:
        cur.execute("SELECT joke_text FROM jokes WHERE id = %s", (joke_id,))
        joke = cur.fetchone()[0]
    return joke

def get_random_trivia():
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM trivia")
        trivia_list = cur.fetchall()
    if not trivia_list:
        return None
    trivia_item = random.choice(trivia_list)
    return trivia_item  # Retorna un diccionario con keys: 'id', 'question', 'answer', 'hint'

######################################
# INICIALIZACIÓN DEL BOT
######################################
intents = discord.Intents.default()
intents.members = True
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)

async def send_public_message(message: str):
    public_channel = bot.get_channel(PUBLIC_CHANNEL_ID)
    if public_channel:
        await public_channel.send(message)
    else:
        print("No se pudo encontrar el canal público.")

######################################
# ENDPOINTS DE LA API PRIVADA
######################################
app = Flask(__name__)

def check_auth(req):
    auth = req.headers.get("Authorization")
    if not auth or auth != f"Bearer {API_SECRET}":
        return False
    return True

@app.route("/", methods=["GET"])
def home_page():
    return "El bot está funcionando!", 200

@app.route("/api/update_points", methods=["POST"])
def api_update_points():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or "member_id" not in data or "points" not in data:
        return jsonify({"error": "Missing parameters"}), 400
    try:
        member_id = int(data["member_id"])
        points = int(data["points"])
    except ValueError:
        return jsonify({"error": "Invalid parameters"}), 400
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return jsonify({"error": "Guild not found"}), 404
    try:
        member = guild.get_member(member_id)
        if member is None:
            member = bot.loop.run_until_complete(guild.fetch_member(member_id))
    except Exception as e:
        return jsonify({"error": "Member not found", "details": str(e)}), 404
    new_points = update_score(member, points)
    bot.loop.create_task(send_public_message(
        f"✅ API: Puntuación actualizada: {member.display_name} ahora tiene {new_points} puntos"))
    return jsonify({"message": "Puntuación actualizada", "new_points": new_points}), 200

@app.route("/api/delete_member", methods=["POST"])
def api_delete_member():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or "member_id" not in data:
        return jsonify({"error": "Missing parameter: member_id"}), 400
    try:
        member_id = int(data["member_id"])
    except ValueError:
        return jsonify({"error": "Invalid member_id"}), 400
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return jsonify({"error": "Guild not found"}), 404
    try:
        member = guild.get_member(member_id)
        if member is None:
            member = bot.loop.run_until_complete(guild.fetch_member(member_id))
    except Exception as e:
        return jsonify({"error": "Member not found", "details": str(e)}), 404
    with conn.cursor() as cur:
        cur.execute("DELETE FROM participants WHERE id = %s", (str(member.id),))
    bot.loop.create_task(send_public_message(
        f"✅ API: {member.display_name} eliminado del torneo"))
    return jsonify({"message": "Miembro eliminado"}), 200

@app.route("/api/set_stage", methods=["POST"])
def api_set_stage():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or "stage" not in data:
        return jsonify({"error": "Missing parameter: stage"}), 400
    try:
        stage = int(data["stage"])
    except ValueError:
        return jsonify({"error": "Invalid stage"}), 400
    global current_stage
    current_stage = stage
    bot.loop.create_task(send_public_message(f"✅ API: Etapa actual configurada a {stage}"))
    return jsonify({"message": "Etapa configurada", "stage": stage}), 200

######################################
# COMANDOS SENSIBLES DE DISCORD (con “!” – Solo el Propietario en el canal autorizado)
######################################
@bot.command()
async def actualizar_puntuacion(ctx, jugador: str, puntos: int):
    if ctx.author.id != OWNER_ID or ctx.channel.id != PUBLIC_CHANNEL_ID:
        try:
            await ctx.message.delete()
        except:
            pass
        return
    match = re.search(r'\d+', jugador)
    if not match:
        await send_public_message("No se pudo encontrar al miembro.")
        await ctx.message.delete()
        return
    member_id = int(match.group())
    guild = ctx.guild or bot.get_guild(GUILD_ID)
    if guild is None:
        await send_public_message("No se pudo determinar el servidor.")
        await ctx.message.delete()
        return
    try:
        member = guild.get_member(member_id)
        if member is None:
            member = await guild.fetch_member(member_id)
    except Exception as e:
        await send_public_message("No se pudo encontrar al miembro en el servidor.")
        await ctx.message.delete()
        return
    try:
        puntos = int(puntos)
    except ValueError:
        await send_public_message("Por favor, proporciona un número válido de puntos.")
        await ctx.message.delete()
        return
    new_points = update_score(member, puntos)
    await send_public_message(f"✅ Puntuación actualizada: {member.display_name} ahora tiene {new_points} puntos")
    try:
        await ctx.message.delete()
    except:
        pass

@bot.command()
async def reducir_puntuacion(ctx, jugador: str, puntos: int):
    if ctx.author.id != OWNER_ID or ctx.channel.id != PUBLIC_CHANNEL_ID:
        try:
            await ctx.message.delete()
        except:
            pass
        return
    await actualizar_puntuacion(ctx, jugador, -puntos)
    try:
        await ctx.message.delete()
    except:
        pass

@bot.command()
async def ver_puntuacion(ctx):
    participant = get_participant(str(ctx.author.id))
    if participant:
        await ctx.send(f"🏆 Tu puntaje del torneo es: {participant.get('puntos', 0)}")
    else:
        await ctx.send("❌ No estás registrado en el torneo")

@bot.command()
async def clasificacion(ctx):
    data = get_all_participants()
    sorted_players = sorted(data["participants"].items(), key=lambda item: int(
        item[1].get("puntos", 0)), reverse=True)
    ranking = "🏅 Clasificación del Torneo:\n"
    for idx, (uid, player) in enumerate(sorted_players, 1):
        ranking += f"{idx}. {player['nombre']} - {player.get('puntos', 0)} puntos\n"
    await ctx.send(ranking)

@bot.command()
async def avanzar_etapa(ctx):
    if ctx.author.id != OWNER_ID or ctx.channel.id != PUBLIC_CHANNEL_ID:
        try:
            await ctx.message.delete()
        except:
            pass
        return
    global current_stage
    current_stage += 1
    data = get_all_participants()
    sorted_players = sorted(data["participants"].items(), key=lambda item: int(
        item[1].get("puntos", 0)), reverse=True)
    cutoff = STAGES.get(current_stage)
    if cutoff is None:
        await send_public_message("No hay configuración para esta etapa.")
        await ctx.message.delete()
        return
    avanzan = sorted_players[:cutoff]
    eliminados = sorted_players[cutoff:]
    for uid, player in avanzan:
        player["etapa"] = current_stage
        upsert_participant(uid, player)
        try:
            member = ctx.guild.get_member(int(uid)) or await ctx.guild.fetch_member(int(uid))
            await member.send(f"🎉 ¡Felicidades! Has avanzado a la etapa {current_stage} ({stage_names.get(current_stage, 'Etapa ' + str(current_stage))}).")
        except Exception as e:
            print(f"Error al enviar mensaje a {uid}: {e}")
    for uid, player in eliminados:
        try:
            member = ctx.guild.get_member(int(uid)) or await ctx.guild.fetch_member(int(uid))
            await member.send(f"❌ Lo siento, has sido eliminado del torneo en la etapa {current_stage - 1}.")
        except Exception as e:
            print(f"Error al enviar mensaje a {uid}: {e}")
    await send_public_message(f"✅ Etapa {current_stage} iniciada. {cutoff} jugadores avanzaron y {len(eliminados)} fueron eliminados.")
    try:
        await ctx.message.delete()
    except:
        pass

@bot.command()
async def retroceder_etapa(ctx):
    if ctx.author.id != OWNER_ID or ctx.channel.id != PUBLIC_CHANNEL_ID:
        try:
            await ctx.message.delete()
        except:
            pass
        return
    global current_stage
    if current_stage <= 1:
        await send_public_message("No se puede retroceder de la etapa 1.")
        try:
            await ctx.message.delete()
        except:
            pass
        return
    current_stage -= 1
    data = get_all_participants()
    for uid, player in data["participants"].items():
        player["etapa"] = current_stage
        upsert_participant(uid, player)
    await send_public_message(f"✅ Etapa retrocedida. Ahora la etapa es {current_stage} ({stage_names.get(current_stage, 'Etapa ' + str(current_stage))}).")
    try:
        await ctx.message.delete()
    except:
        pass

@bot.command()
async def eliminar_jugador(ctx, jugador: str):
    if ctx.author.id != OWNER_ID or ctx.channel.id != PUBLIC_CHANNEL_ID:
        try:
            await ctx.message.delete()
        except:
            pass
        return
    match = re.search(r'\d+', jugador)
    if not match:
        await send_public_message("No se pudo encontrar al miembro.")
        await ctx.message.delete()
        return
    member_id = int(match.group())
    guild = ctx.guild or bot.get_guild(GUILD_ID)
    if guild is None:
        await send_public_message("No se pudo determinar el servidor.")
        await ctx.message.delete()
        return
    try:
        member = guild.get_member(member_id) or await guild.fetch_member(member_id)
    except Exception as e:
        await send_public_message("No se pudo encontrar al miembro en el servidor.")
        await ctx.message.delete()
        return
    user_id = str(member.id)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM participants WHERE id = %s", (user_id,))
    await send_public_message(f"✅ {member.display_name} eliminado del torneo")
    try:
        await ctx.message.delete()
    except:
        pass

@bot.command()
async def configurar_etapa(ctx, etapa: int):
    if ctx.author.id != OWNER_ID or ctx.channel.id != PUBLIC_CHANNEL_ID:
        try:
            await ctx.message.delete()
        except:
            pass
        return
    global current_stage
    current_stage = etapa
    await send_public_message(f"✅ Etapa actual configurada a {etapa}")
    try:
        await ctx.message.delete()
    except:
        pass

# Comando !trivia (disponible para el owner; los demás inician trivia por lenguaje natural)
@bot.command()
async def trivia(ctx):
    if ctx.author.id != OWNER_ID or ctx.channel.id != PUBLIC_CHANNEL_ID:
        try:
            await ctx.message.delete()
        except:
            pass
        return
    trivia_item = get_random_trivia()
    if trivia_item is None:
        await ctx.send("No hay trivias disponibles.")
        return
    active_trivia[ctx.channel.id] = trivia_item
    await ctx.send(f"**Trivia:** {trivia_item['question']}\n_Responde en el chat._")
    try:
        await ctx.message.delete()
    except:
        pass

@bot.command()
async def chiste(ctx):
    await ctx.send(get_random_joke())

######################################
# COMANDOS DE ADMINISTRACIÓN PARA CHISTES Y TRIVIAS
######################################

# Verificamos si el mensaje es un DM del OWNER
def is_owner_dm(ctx):
    return ctx.author.id == OWNER_ID and isinstance(ctx.channel, discord.DMChannel)

@bot.command()
async def agregar_chiste(ctx, *, chiste: str):
    if not is_owner_dm(ctx):
        # Si no es el OWNER o no es un DM, ignoramos y borramos el mensaje si es posible
        try:
            await ctx.message.delete()
        except:
            pass
        return
    with conn.cursor() as cur:
        cur.execute("INSERT INTO jokes (joke_text) VALUES (%s)", (chiste,))
    await ctx.send("✅ Chiste agregado exitosamente.")

@bot.command()
async def eliminar_chiste(ctx, joke_id: int):
    if not is_owner_dm(ctx):
        try:
            await ctx.message.delete()
        except:
            pass
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM jokes WHERE id = %s", (joke_id,))
    await ctx.send(f"✅ Chiste con ID {joke_id} eliminado.")

@bot.command()
async def listar_chistes(ctx):
    if not is_owner_dm(ctx):
        try:
            await ctx.message.delete()
        except:
            pass
        return
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, joke_text FROM jokes ORDER BY id")
        jokes = cur.fetchall()
    if not jokes:
        await ctx.send("No hay chistes en la base de datos.")
        return
    messages = []
    for joke in jokes:
        messages.append(f"ID: {joke['id']} - {joke['joke_text']}")
    # Enviar los chistes en segmentos para evitar exceder el límite de Discord
    for i in range(0, len(messages), 10):
        chunk = "\n".join(messages[i:i+10])
        await ctx.send(chunk)

@bot.command()
async def agregar_trivia(ctx):
    if not is_owner_dm(ctx):
        try:
            await ctx.message.delete()
        except:
            pass
        return
    def check(m):
        return m.author.id == OWNER_ID and isinstance(m.channel, discord.DMChannel)
    await ctx.send("Por favor, envía la pregunta de la trivia:")
    try:
        question_msg = await bot.wait_for('message', check=check, timeout=60)
        question = question_msg.content.strip()
        await ctx.send("Ahora, envía la respuesta correcta:")
        answer_msg = await bot.wait_for('message', check=check, timeout=60)
        answer = answer_msg.content.strip()
        await ctx.send("Finalmente, envía la pista de ayuda:")
        hint_msg = await bot.wait_for('message', check=check, timeout=60)
        hint = hint_msg.content.strip()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trivia (question, answer, hint)
                VALUES (%s, %s, %s)
            """, (question, answer, hint))
        await ctx.send("✅ Trivia agregada exitosamente.")
    except asyncio.TimeoutError:
        await ctx.send("⏰ Tiempo de espera excedido. Por favor, intenta nuevamente.")

@bot.command()
async def eliminar_trivia(ctx, trivia_id: int):
    if not is_owner_dm(ctx):
        try:
            await ctx.message.delete()
        except:
            pass
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM trivia WHERE id = %s", (trivia_id,))
    await ctx.send(f"✅ Trivia con ID {trivia_id} eliminada.")

@bot.command()
async def listar_trivias(ctx):
    if not is_owner_dm(ctx):
        try:
            await ctx.message.delete()
        except:
            pass
        return
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, question, answer, hint FROM trivia ORDER BY id")
        trivias = cur.fetchall()
    if not trivias:
        await ctx.send("No hay trivias en la base de datos.")
        return
    messages = []
    for trivia in trivias:
        messages.append(f"ID: {trivia['id']}\nPregunta: {trivia['question']}\nRespuesta: {trivia['answer']}\nPista: {trivia['hint']}\n")
    # Enviar las trivias en segmentos para evitar exceder el límite de Discord
    for i in range(0, len(messages), 2):
        chunk = "\n".join(messages[i:i+2])
        await ctx.send(chunk)

######################################
# COMANDOS PARA AGREGAR CHISTES Y TRIVIAS EN MASA
######################################

@bot.command()
async def agregar_chistes_masa(ctx):
    if not is_owner_dm(ctx):
        # Si no es el OWNER o no es un DM, ignoramos y borramos el mensaje si es posible
        try:
            await ctx.message.delete()
        except:
            pass
        return
    await ctx.send("Por favor, envía todos los chistes que deseas agregar, cada uno en una línea separada. Cuando termines, escribe `FIN`.")

    def check(m):
        return m.author.id == OWNER_ID and isinstance(m.channel, discord.DMChannel)

    jokes = []
    while True:
        try:
            msg = await bot.wait_for('message', check=check, timeout=300)  # Espera hasta 5 minutos
            if msg.content.strip().upper() == 'FIN':
                break
            else:
                # Dividimos el mensaje en líneas si es que enviaste varios chistes juntos
                lines = msg.content.strip().split('\n')
                jokes.extend([line.strip() for line in lines if line.strip()])
        except asyncio.TimeoutError:
            await ctx.send("⏰ Tiempo de espera excedido. Por favor, intenta nuevamente.")
            return
    if jokes:
        # Insertar los chistes en la base de datos
        with conn.cursor() as cur:
            for joke in jokes:
                cur.execute("INSERT INTO jokes (joke_text) VALUES (%s)", (joke,))
        await ctx.send(f"✅ Se han agregado {len(jokes)} chistes exitosamente.")
    else:
        await ctx.send("No se agregaron chistes.")

@bot.command()
async def agregar_trivias_masa(ctx):
    if not is_owner_dm(ctx):
        try:
            await ctx.message.delete()
        except:
            pass
        return
    await ctx.send("Por favor, envía las trivias que deseas agregar en el siguiente formato:\n`Pregunta::Respuesta::Pista`\nEnvía una trivia por línea. Cuando termines, escribe `FIN`.")

    def check(m):
        return m.author.id == OWNER_ID and isinstance(m.channel, discord.DMChannel)

    trivia_list = []
    while True:
        try:
            msg = await bot.wait_for('message', check=check, timeout=600)  # Espera hasta 10 minutos
            if msg.content.strip().upper() == 'FIN':
                break
            else:
                lines = msg.content.strip().split('\n')
                for line in lines:
                    parts = line.strip().split("::")
                    if len(parts) != 3:
                        await ctx.send(f"Formato incorrecto en línea:\n`{line}`\nAsegúrate de usar `Pregunta::Respuesta::Pista`.")
                        continue
                    question, answer, hint = parts
                    trivia_list.append({'question': question.strip(), 'answer': answer.strip(), 'hint': hint.strip()})
        except asyncio.TimeoutError:
            await ctx.send("⏰ Tiempo de espera excedido. Por favor, intenta nuevamente.")
            return
    if trivia_list:
        # Insertar las trivias en la base de datos
        with conn.cursor() as cur:
            for trivia in trivia_list:
                cur.execute("""
                    INSERT INTO trivia (question, answer, hint)
                    VALUES (%s, %s, %s)
                """, (trivia['question'], trivia['answer'], trivia['hint']))
        await ctx.send(f"✅ Se han agregado {len(trivia_list)} trivias exitosamente.")
    else:
        await ctx.send("No se agregaron trivias.")

######################################
# NUEVOS COMANDOS PARA FECHAS Y NOTIFICACIONES
######################################

@bot.command()
async def crear_noti(ctx, fecha: str, hora: str, destinatarios: str, *, mensaje: str):
    if ctx.author.id != OWNER_ID or ctx.channel.id != PUBLIC_CHANNEL_ID:
        try:
            await ctx.message.delete()
        except:
            pass
        return
    # Eliminar el mensaje después de procesarlo
    try:
        await ctx.message.delete()
    except:
        pass
    # Parsear la fecha y hora
    datetime_str = f"{fecha} {hora}"
    fecha_hora = dateparser.parse(datetime_str, languages=['es'])
    if not fecha_hora:
        await ctx.send("❌ Fecha y hora no válidas. Por favor, utiliza el formato correcto.")
        return
    # Verificar si la fecha es futura
    if fecha_hora <= datetime.datetime.now():
        await ctx.send("❌ La fecha y hora deben ser futuras.")
        return
    # Guardar en la base de datos
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO notifications (scheduled_time, recipients, message)
            VALUES (%s, %s, %s)
        """, (fecha_hora, destinatarios.lower(), mensaje))
    await ctx.send(f"✅ Notificación programada para {fecha_hora.strftime('%d/%m/%Y %H:%M')}.")

@bot.command()
async def crear_fecha(ctx, fecha: str, hora: str, *, descripcion: str):
    if ctx.author.id != OWNER_ID or not isinstance(ctx.channel, discord.DMChannel):
        try:
            await ctx.message.delete()
        except:
            pass
        return
    # Parsear la fecha y hora
    datetime_str = f"{fecha} {hora}"
    fecha_hora = dateparser.parse(datetime_str, languages=['es'])
    if not fecha_hora:
        await ctx.send("❌ Fecha y hora no válidas.")
        return
    # Guardar en la base de datos
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO calendar_events (event_time, description)
            VALUES (%s, %s)
        """, (fecha_hora, descripcion))
    await ctx.send(f"✅ Fecha creada: {fecha_hora.strftime('%d/%m/%Y %H:%M')} - {descripcion}")

@bot.command()
async def consulta_fechas(ctx):
    if ctx.author.id != OWNER_ID or not isinstance(ctx.channel, discord.DMChannel):
        try:
            await ctx.message.delete()
        except:
            pass
        return
    with conn.cursor() as cur:
        cur.execute("""
            SELECT event_time, description FROM calendar_events ORDER BY event_time ASC
        """)
        events = cur.fetchall()
    if not events:
        await ctx.send("📅 No hay fechas en el calendario.")
        return
    response = "📅 **Fechas Programadas:**\n"
    for event in events:
        fecha_formateada = event[0].strftime('%d/%m/%Y %H:%M')
        response += f"📌 {fecha_formateada} - {event[1]}\n"
    await ctx.send(response)

######################################
# EVENTO ON_MESSAGE: Comandos de Lenguaje Natural
######################################
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Si el mensaje es una mención al bot
    if bot.user in message.mentions:
        if message.author.id == OWNER_ID:
            await message.channel.send("holi")
        else:
            # Mostrar los comandos de lenguaje natural disponibles
            help_text = (
                "**Comandos Disponibles:**\n"
                "   - **ranking**\n"
                "   - **topmejores**\n"
                "   - **misestrellas**\n"
                "   - **topestrellas**\n"
                "   - **chiste**\n"
                "   - **trivia**\n"
                "   - **oráculo**\n"
                "   - **meme**\n"
                "   - **piedra papel tijeras**\n"
                "   - **duelo de chistes contra @usuario**\n"
                "   - **fechas**\n"
            )
            await message.channel.send(help_text)
        return

    # Permitir comandos de lenguaje natural en cualquier canal y mensajes privados
    global stage_names, current_stage, active_trivia

    def normalize_string_local(s):
        return ''.join(c for c in unicodedata.normalize('NFKD', s)
                       if not unicodedata.combining(c)).replace(" ", "").lower()

    content = message.content.strip().lower()

    # Comando de lenguaje natural "fechas"
    if content == "fechas":
        with conn.cursor() as cur:
            cur.execute("""
                SELECT event_time, description FROM calendar_events
                WHERE event_time >= %s
                ORDER BY event_time ASC
            """, (datetime.datetime.now(),))
            events = cur.fetchall()
        if not events:
            await message.channel.send("📅 No hay fechas próximas en el calendario.")
            return
        response = "📅 **Próximas Fechas:**\n"
        for event in events:
            fecha_formateada = event[0].strftime('%d/%m/%Y %H:%M')
            response += f"📌 {fecha_formateada} - {event[1]}\n"
        await message.channel.send(response)
        return

    # ... (Resto de los comandos de lenguaje natural existentes)
    # Asegúrate de que los comandos pueden utilizarse en cualquier canal o mensajes privados

    # Procesar los demás comandos de lenguaje natural y comandos existentes
    # ... (Código existente)

    await bot.process_commands(message)

######################################
# TAREA PARA ENVIAR NOTIFICACIONES PROGRAMADAS
######################################
async def check_notifications():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.datetime.now()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM notifications WHERE scheduled_time <= %s
            """, (now,))
            notifications = cur.fetchall()
            for noti in notifications:
                # Determinar los destinatarios
                if noti['recipients'] == 'todos':
                    guild = bot.get_guild(GUILD_ID)
                    if guild:
                        members = guild.members
                elif 'etapa' in noti['recipients']:
                    etapa_num = int(noti['recipients'].split(' ')[1])
                    cur.execute("""
                        SELECT id FROM participants WHERE etapa = %s
                    """, (etapa_num,))
                    participant_ids = [row[0] for row in cur.fetchall()]
                    members = [bot.get_user(int(uid)) for uid in participant_ids]
                else:
                    members = []
                # Enviar el mensaje
                for member in members:
                    if member:
                        try:
                            await member.send(noti['message'])
                        except Exception as e:
                            print(f"Error al enviar notificación a {member}: {e}")
                # Eliminar la notificación una vez enviada
                cur.execute("DELETE FROM notifications WHERE id = %s", (noti['id'],))
        await asyncio.sleep(60)  # Esperar 1 minuto antes de revisar nuevamente

bot.loop.create_task(check_notifications())

######################################
# EVENTO ON_READY
######################################
@bot.event
async def on_ready():
    print(f'Bot conectado como {bot.user.name}')

######################################
# SERVIDOR WEB PARA MANTENER EL BOT ACTIVO (API PRIVADA)
######################################
def run_webserver():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == '__main__':
    threading.Thread(target=run_webserver).start()
    bot.run(os.getenv('DISCORD_TOKEN'))
