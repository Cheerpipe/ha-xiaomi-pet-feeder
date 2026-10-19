# Xiaomi Smart Pet Feeder para Home Assistant

<img src="docs/images/feeder.png" alt="Xiaomi Smart Pet Food Feeder 2" align="right" width="180">

*[Read in English](README.md)*

Integración nativa de Home Assistant para el **Xiaomi Smart Pet Food Feeder 2** (`xiaomi.feeder.iv2001`, y su gemelo chino `xiaomi.feeder.pi2001`).

La hice porque las integraciones genéricas de Xiaomi se quedaban a medias. Podía dispensar, pero "comida consumida hoy" siempre marcaba 0. El desecante no aparecía. Y después de un corte de luz el botón de dispensar soltaba 100 g en vez de 10.

Resulta que la app Mi Home no lee esos datos de las propiedades MIoT estándar, sino de un servicio aparte de Xiaomi. Así que desarmé el plugin oficial de la app (versiones 1.0.93 y 1.1.8), comprobé cada petición contra un comedero real y replico aquí el mismo comportamiento. La idea es sencilla: **lo que muestra la app, lo muestra Home Assistant**.

## Qué incluye

**Sensores**
- **Comida consumida hoy**, en gramos. La calcula la nube de Xiaomi y es el mismo número de la app.
- **Comida que queda en el cuenco**: el peso del cuenco en vivo.
- **Progreso del programa de alimentación**, en %.
- **Estado**: un único valor con la misma prioridad que la animación de la app (dispensando, atasco, comida insuficiente, error del cuenco…).
- **Estado del cartucho desecante**: suficiente, insuficiente o agotado, más nivel y días restantes.
- **Registro de alimentación de hoy**: cada comida y dispensación del día, en los atributos.

**Alertas** (sensores binarios)
- Atasco de comida
- Comida insuficiente
- Error del cuenco
- Bajo consumo de comida
- Es el momento de rellenar
- Error del sensor de peso
- Cuenco lleno
- Dispensando
- Pilas de respaldo (desactivada por defecto)

**Controles**
- Botón **Dispensar**, junto a un número **Porciones a dispensar** (1–15, unos 10 g cada una)
- Interruptores:
  - Alimentación automática
  - Notificación de bajo consumo
  - Recordatorio de rellenado
  - Corrección de errores de dispensación
  - Prevención de derrames en el cuenco
  - Apagado automático de la pantalla
  - Bloqueo infantil
- Números: umbral de bajo consumo (10–90 %) y duración del cuenco vacío (6–12 h)
- Selector: qué muestra la pantalla del comedero (gramos restantes, gramos ingeridos hoy o progreso del programa)
- Botones: calibración del peso, descartar avisos y reiniciar desecante

**Eventos y calendario**
- **Entidades de evento**: saltan cuando se dispensa comida (con origen, resultado y porciones) y cuando tu mascota come (con los gramos). Sirven de disparador para automatizaciones.
- **Calendario "Alimentación"**: muestra el programa de los próximos días y el historial de los últimos 90.

**Servicios**

| Servicio | Qué hace |
|---|---|
| `xiaomi_pet_feeder.dispense` | Dispensa N porciones ahora |
| `xiaomi_pet_feeder.set_schedule` | Reemplaza todo el programa diario |
| `xiaomi_pet_feeder.add_schedule_entry` / `remove_schedule_entry` | Añade o quita una comida |
| `xiaomi_pet_feeder.set_schedule_entry_today` | Salta (o recupera) una comida solo por hoy, como la app |
| `xiaomi_pet_feeder.get_schedule` | Devuelve el programa |
| `xiaomi_pet_feeder.get_feeding_history` | Devuelve comidas y dispensaciones, hasta 90 días |
| `xiaomi_pet_feeder.get_hourly_stats` / `get_daily_stats` | Los datos de los gráficos diario y semanal de la app |

Los cambios al programa siguen las reglas de la app: como máximo 35 comidas, con al menos 5 minutos entre una y otra, y 1–15 porciones cada una.

## Instalación

### HACS (recomendado)

1. En HACS, abre **⋮ → Repositorios personalizados**.
2. Añade `https://github.com/Cheerpipe/ha-xiaomi-pet-feeder` con el tipo **Integración**.
3. Instala **Xiaomi Smart Pet Feeder** y reinicia Home Assistant.

### Manual

Copia `custom_components/xiaomi_pet_feeder` en tu carpeta `config/custom_components` y reinicia.

**Requiere Home Assistant 2026.10 o superior.**

## Configuración

1. Ve a **Ajustes → Dispositivos y servicios → Añadir integración → Xiaomi Smart Pet Feeder**.
2. Elige la **región**. Tiene que ser la misma que en la app Xiaomi Home (*Perfil → Ajustes → Región*). En Latinoamérica suele ser `us` o `sg`; en Europa, `de`.
3. Elige cómo iniciar sesión:
   - **Usuario y contraseña.** Si Xiaomi pide captcha o código de verificación (email o SMS), la siguiente pantalla te lo pide.
   - **Código QR.** Escanéalo desde la app Xiaomi Home (pestaña *Perfil* → icono de escanear) y confirma en el móvil. Úsalo si el login con contraseña falla; Xiaomi se ha vuelto exigente con eso.
4. Selecciona tus comederos y listo.

La contraseña se usa una sola vez y **no se guarda**. Home Assistant solo conserva los tokens de sesión y los renueva solo cuando caducan. Si un día no puede renovarlos, verás un aviso para volver a iniciar sesión.

## Cómo funciona y sus límites

- **Funciona por la nube y consulta periódicamente (polling).**
  - El estado del comedero (peso del cuenco, alertas, ajustes) se lee cada **30 segundos**.
  - "Comida consumida hoy" y el registro, cada **5 minutos**.
  - El desecante, cada 6 horas.
  - Los dos primeros intervalos se cambian en el botón **Configurar** de la integración.
- **No tiene push en tiempo real propio.** Xiaomi solo ofrece push a su integración oficial. Por sí sola, esta integración puede mostrar los eventos (comió, dispensó) unos minutos después. Si también tienes la integración oficial, mira [Actualizaciones en tiempo real](#actualizaciones-en-tiempo-real-opcional). Lo que hagas desde Home Assistant se refresca al instante.
- **El comedero sube cada comida cuando la mascota se aleja.** Incluso la app las recibe con un pequeño retraso.
- **Usa APIs no documentadas de Xiaomi.** Hoy funcionan, pero Xiaomi puede cambiarlas sin aviso. Si algo se rompe, abre un issue y adjunta los diagnósticos de la integración; ya vienen anonimizados.

## Actualizaciones en tiempo real (opcional)

Xiaomi envía los eventos del dispositivo en tiempo real solo a su integración oficial [Xiaomi Home](https://github.com/XiaoMi/ha_xiaomi_home). Si también la tienes instalada y en ella está el mismo comedero, esta integración aprovecha ese push:

- Vigila las entidades de evento del comedero en la integración oficial: la de comida (`event.*_e_2_1`) y las de dispensación (`event.*_e_2_3`, `event.*_e_2_5`).
- Cuando una se dispara, refresca al momento la comida consumida hoy, el registro y el estado, sin esperar a la siguiente consulta. Vuelve a mirar 20 segundos después por si la nube de Xiaomi guarda el registro con algo de retraso.
- Solo lee el estado de esas entidades. Nunca toca la conexión de la integración oficial con Xiaomi, así que sus entidades siguen funcionando igual.
- El polling sigue activo como respaldo. Si quitas la integración oficial o sus entidades no están disponibles, no se rompe nada; simplemente se vuelve a los intervalos de consulta.

Viene **activado por defecto** y no hace nada hasta que existan las entidades oficiales. Si instalas la integración oficial más tarde, también las detecta. Se desactiva en el botón **Configurar** de la integración (*Actualizaciones en tiempo real desde Xiaomi Home*). Esa pantalla también indica cuántos de tus comederos se encontraron en la integración oficial. Si sale 0, revisa que el comedero esté añadido allí y que sus entidades de evento estén activadas.

## Problemas frecuentes

- **"No se encontró ningún Smart Pet Food Feeder 2"**: la región no es la correcta. Prueba otra.
- **El login falla una y otra vez**: usa el código QR.
- **Logs detallados**: añade esto a `configuration.yaml`:
  ```yaml
  logger:
    logs:
      custom_components.xiaomi_pet_feeder: debug
  ```

## Agradecimientos

- [@Wh1terat](https://github.com/Wh1terat), por descifrar la API del comedero en [al-one/hass-xiaomi-miot#2415](https://github.com/al-one/hass-xiaomi-miot/issues/2415).
- [al-one/hass-xiaomi-miot](https://github.com/al-one/hass-xiaomi-miot) y [PiotrMachowski/Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor), por años de trabajo con el login de Xiaomi.

Este proyecto no está afiliado ni respaldado por Xiaomi. "Xiaomi" y "Mi Home" son marcas de sus respectivos dueños.

## Licencia

[MIT](LICENSE)
