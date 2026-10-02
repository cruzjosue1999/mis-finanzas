# Mis Finanzas

App personal (PWA instalable en la pantalla principal) para seguir ingresos y
gastos mes a mes, con calendario y pagos a compañías. Moneda: USD.

- **Panel**: tarjetas de Ingresos / Gastos / Saldo del mes, gráfico de gastos
  por categoría, evolución mensual del año y resumen anual.
- **Movimientos**: lista de ingresos y gastos con filtros, agregar / editar / borrar.
- **Calendario**: vista mensual con los montos de cada día y los pagos a compañías.
- **Compañías**: pagos pendientes y pagados; al marcar uno mensual como pagado
  se genera automáticamente el del mes siguiente.

## Desarrollo local

```bash
./start.sh            # http://localhost:8090
```

## Despliegue (Render)

Servicio web `mis-finanzas` (plan gratuito) con las variables de entorno
`TURSO_URL` y `TURSO_TOKEN` (base de datos Turso `mis-finanzas`, réplica
embebida libsql). El auto-deploy de Render no siempre se dispara solo: usar
"Manual Deploy → Deploy latest commit" desde el dashboard.
