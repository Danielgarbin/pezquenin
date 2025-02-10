# bot_notificaciones.py

import discord
from discord.ext import commands
import psycopg2
import psycopg2.extras
import os
import dateparser
import datetime
import asyncio

######################################
# CONFIGURACIÓN: IDs y Servidor
######################################
OWNER_ID = 1336609089656197171  # Reemplaza con tu Discord ID
GUILD_ID = 123456789012345678  # Reemplaza con el ID de tu servidor

######################################
# CONEXIÓN A LA BASE DE DATOS POSTGRESQL
######################################
DATABASE_URL = os.environ.get("DATABASE_URL")  # La variable de entorno en Render
conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True

def init_db():
    with conn.cursor() as cur:
        # Tabla de notificaciones
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
# CONFIGURACIÓN DEL BOT
######################################
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

######################################
# COMANDOS DEL BOT
######################################
@bot.command()
async def crear_noti(ctx, fecha: str, hora: str, destinatarios: str, *, mensaje: str):
    if ctx.author.id != OWNER_ID:
        try:
            await ctx.message.delete()
        except:
            pass
        return
    try:
        await ctx.message.delete()
    except:
        pass
    # Parsear fecha y hora
    datetime_str = f"{fecha} {hora}"
    fecha_hora = dateparser.parse(datetime_str, languages=['es'])
    if not fecha_hora:
        await ctx.send("❌ Fecha y hora no válidas. Por favor, utiliza el formato correcto.")
        return
    if fecha_hora <= datetime.datetime.now():
        await ctx.send("❌ La fecha y hora deben ser futuras.")
        return
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO notifications (scheduled_time, recipients, message)
            VALUES (%s, %s, %s)
        """, (fecha_hora, destinatarios.lower(), mensaje))
    await ctx.send(f"✅ Notificación programada para {fecha_hora.strftime('%d/%m/%Y %H:%M')}.")

@bot.command()
async def crear_fecha(ctx, fecha: str, hora: str, *, descripcion: str):
    if ctx.author.id != OWNER_ID:
        try:
            await ctx.message.delete()
        except:
            pass
        return
    try:
        await ctx.message.delete()
    except:
        pass
    datetime_str = f"{fecha} {hora}"
    fecha_hora = dateparser.parse(datetime_str, languages=['es'])
    if not fecha_hora:
        await ctx.send("❌ Fecha y hora no válidas.")
        return
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO calendar_events (event_time, description)
            VALUES (%s, %s)
        """, (fecha_hora, descripcion))
    await ctx.send(f"✅ Fecha creada: {fecha_hora.strftime('%d/%m/%Y %H:%M')} - {descripcion}")

@bot.command()
async def fechas(ctx):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT event_time, description FROM calendar_events
            WHERE event_time >= %s
            ORDER BY event_time ASC
        """, (datetime.datetime.now(),))
        events = cur.fetchall()
    if not events:
        await ctx.send("📅 No hay fechas próximas en el calendario.")
        return
    response = "📅 **Próximas Fechas:**\n"
    for event in events:
        fecha_formateada = event[0].strftime('%d/%m/%Y %H:%M')
        response += f"📌 {fecha_formateada} - {event[1]}\n"
    await ctx.send(response)

######################################
# TAREA ASÍNCRONA: Enviar Notificaciones
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
                else:
                    members = []
                # Enviar el mensaje
                for member in members:
                    if member:
                        try:
                            await member.send(noti['message'])
                        except Exception as e:
                            print(f"Error al enviar notificación a {member}: {e}")
                # Eliminar la notificación de la base de datos
                cur.execute("DELETE FROM notifications WHERE id = %s", (noti['id'],))
        await asyncio.sleep(60)  # Espera 1 minuto antes de verificar nuevamente

bot.loop.create_task(check_notifications())

######################################
# EVENTO ON_READY
######################################
@bot.event
async def on_ready():
    print(f'Bot de Notificaciones conectado como {bot.user.name}')

######################################
# INICIAR EL BOT
######################################
bot.run(os.getenv('DISCORD_TOKEN'))

