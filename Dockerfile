# Usar una imagen base de Python 3.8
FROM python:alpine

# Establecer el directorio de trabajo en /app
WORKDIR /app

# Copiar ficheros al contenedor
COPY requirements.txt .
COPY dmiot2mqtt.py .
COPY discovery.json .

# Instalar las dependencias
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 31270

# Comando para ejecutar el script de Python
ENTRYPOINT [ "python3"]
CMD ["dmiot2mqtt.py", "-c", "/config/dmiot2mqtt.ini"]
