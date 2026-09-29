# Gestor de copy trading en eToro (papel)

Copia en papel a 10 Popular Investors de eToro elegidos por su regularidad: semanas y meses en verde de los
últimos 2 años. Es la regla que en la prueba fuera de muestra de sep-2025 a sep-2026 dio +19,9 % con 10 traders
del decil superior (hallazgo h416 del cuaderno de investigación). La rentabilidad pasada, en cambio, predijo al revés.

**No hay dinero real ni claves de eToro.** Cada copia se valora con la rentabilidad que eToro publica de cada trader.

## Qué hace

- **Cada día (06:30 UTC):**
  - valora las copias;
  - si una pierde el 35 %, la cierra, la sustituye por la siguiente de la lista y veta a ese trader 3 meses;
  - regenera el panel.
- **El día 1 de cada mes:**
  - rehace la selección;
  - cierra las copias de quien ya no está entre el mejor 20 % o ya no pasa los filtros;
  - abre copias de los siguientes.
- **Telegram:** un mensaje por cada movimiento y el resumen mensual, con el mes y el acumulado desde el 1-10-2026
  frente al MSCI World.

## Comparación y regla para pasar a dinero real

- **Todo va en euros:** las copias rinden en dólares, así que su valor en euros incluye el cambio euro/dólar desde el día
  en que se abrió cada una. El stop sigue midiéndose sobre la rentabilidad del trader en dólares, como el de eToro.
- **La comparación que importa es con el MSCI World en euros** (ETF URTH de Yahoo Finance pasado a euros): la cartera se
  mueve como media bolsa (beta 0,5 frente al S&P 500), así que ganarle al trader mediano no basta.
- **Regla fijada el 29-9-2026, antes de ver resultados** (`regla_para_real` en `config.json`): el 1-4-2027 la cartera en
  euros debe ir al menos 0 pt por delante del MSCI World en euros, contando desde el 1-10-2026. Ese día llega el
  veredicto por Telegram. No se toca a mitad de camino: cambiarla viendo los resultados la invalida.

## Dónde se toca

- `config.json`:
  - capital, número de traders, stop, coste supuesto por apertura y cierre, día del cambio;
  - margen para no rotar y filtros.
- `estado.json`: la cartera. No se edita a mano.
- `docs/index.html`: el panel, publicado en GitHub Pages.

## A mano

```
python gestor.py --estado          ver la cartera sin tocar nada
python gestor.py --sin-telegram    ejecutar sin mandar mensajes
python gestor.py --forzar-cambio   rehacer la selección hoy
python gestor.py --prueba          mensaje de prueba a Telegram
```

## Límites conocidos

- El 0,25 % de coste por lado es una suposición: copiar acciones sin apalancamiento en eToro no tiene comisión
  explícita, pero sí diferencial, y en cripto cobra un 1 %.
- La prueba en que se basa es de un solo año, y el ranking solo muestra a los traders que siguen activos.
