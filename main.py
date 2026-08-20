import asyncio
import os
from playwright.async_api import async_playwright
from dotenv import load_dotenv

load_dotenv()

USUARIO = os.getenv("USUARIO", "mauricio")
PASSWORD = os.getenv("PASSWORD", "mauricio75")
URL = "https://reparacionespaez.sistemasici.es/"

async def ejecutar_bot():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()

        print("1. Accediendo a SICI...")
        await page.goto(URL)

        print("2. Iniciando sesión...")
        await page.fill("input[name='usuario']", USUARIO)
        await page.fill("input[name='contrasenya']", PASSWORD)
        await page.click("button[name='ENTRAR']")

        try:
            await page.wait_for_url("**/principalOperarios.php", timeout=15000)
        except:
            await page.wait_for_load_state("networkidle")

        print("✅ Login exitoso.")

        print("3. Abriendo menú de CADUCADOS...")
        caducados_btn = page.locator("button.btnCaducados").first
        if await caducados_btn.count() > 0:
            await caducados_btn.click()
            await page.wait_for_timeout(2000)

        print("4. Entrando en 'CADUCADOS PDT. CITA'...")
        botones_menu = page.locator("button")
        count = await botones_menu.count()
        for i in range(count):
            btn = botones_menu.nth(i)
            texto = await btn.inner_text()
            if "PDT. CITA" in texto.upper():
                await btn.click()
                break
        
        print("5. Esperando a que el servidor cargue la lista de expedientes...")
        try:
            await page.locator("button.accordion-button").first.wait_for(timeout=15000)
        except Exception:
            print("⚠️ No se encontraron expedientes.")
            await browser.close()
            return

        total_partes = await page.evaluate("document.querySelectorAll(\"button[onclick*='abrirParteV2']\").length")
        print(f"📋 ¡Se han detectado exactamente {total_partes} partes reales para procesar!")

        for i in range(total_partes):
            print(f"\n--- Procesando Parte #{i + 1} de {total_partes} ---")
            
            exito = await page.evaluate(f"""(index) => {{
                let accordions = document.querySelectorAll("button.accordion-button");
                if (accordions[index]) {{
                    if (accordions[index].getAttribute("aria-expanded") !== "true") {{
                        accordions[index].click();
                    }}
                }}
                
                let botonesAbrir = document.querySelectorAll("button[onclick*='abrirParteV2']");
                if (botonesAbrir[index]) {{
                    botonesAbrir[index].click();
                    return true;
                }}
                return false;
            }}""", i)

            if not exito:
                print("   ⚠️ No hay más partes disponibles.")
                break

            try:
                await page.locator("button:has-text('VOLVER'), .btn-success:has-text('VOLVER')").first.wait_for(timeout=15000)
            except:
                print("   ⚠️ La vista tardó en cargar, esperando unos segundos extra...")
                await page.wait_for_timeout(3000)

            print("   ➡️ Extrayendo datos del parte...")
            detalles_parte = await page.evaluate("document.body.innerText;")
            
            nombre_archivo = f"parte_detallado_{i + 1}.txt"
            with open(nombre_archivo, "w", encoding="utf-8") as f:
                f.write(detalles_parte)
            print(f"   ✅ Datos guardados con éxito en '{nombre_archivo}'.")

            print("   ➡️ Volviendo a la lista de caducados...")
            volver_btn = page.locator("button:has-text('VOLVER'), .btn-success:has-text('VOLVER')").first
            if await volver_btn.count() > 0:
                await volver_btn.click()
            else:
                await page.evaluate("history.back();")
                
            await page.wait_for_timeout(3000)

        print("\n🎉 ¡Proceso completado de todos los partes con éxito!")
        await browser.close()

async def main():
    while True:
        try:
            print("🚀 Iniciando ciclo de revisión en SICI...")
            await ejecutar_bot()
        except Exception as e:
            print(f"❌ Error durante la ejecución: {e}")
        
        print("⏳ Esperando 30 minutos para el siguiente ciclo...")
        await asyncio.sleep(1800) # Se repetirá cada 30 minutos automáticamente

if __name__ == "__main__":
    asyncio.run(main())
