import "@testing-library/jest-dom";
import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import SolicitudEnviadaPage from "../page";
import { resendAccessRequestVerification } from "@/services/accessRequest.service";

/**
 * `/solicitud-enviada` es el final del único camino de entrada al producto, y
 * lo transitan visitantes anónimos que no tienen a quién preguntarle. Todo lo
 * que se prueba acá es la misma propiedad: **la pantalla no puede ser un
 * callejón sin salida**.
 *
 * Dos formas de serlo, las dos reales:
 *
 * 1. El reenvío era de un solo uso. `"enviado"` es la primera rama del ternario
 *    y el estado nunca volvía a `"idle"`, así que después del primer reenvío el
 *    botón no se renderizaba nunca más — ni cuando el contador llegaba a cero,
 *    justo después de haber prometido "podés pedir otro en N segundos".
 * 2. Un falso positivo del anti-bot (el honeypot lo llena a veces el autofill)
 *    hace que el backend descarte el envío sin persistir nada y devuelva el
 *    mismo 201 genérico. La pantalla afirma "te mandamos un link" y no hay tal
 *    mail; el reenvío tampoco hace nada, y responde 200 igual por neutralidad
 *    de enumeración. Distinguirlo desde el cliente rompería esa neutralidad, así
 *    que la salida honesta —volver a mandar el formulario— tiene que estar
 *    SIEMPRE, en todas las ramas.
 */

let searchParams = new URLSearchParams("email=ana%40gmail.com");

jest.mock("next/navigation", () => ({
  useSearchParams: () => searchParams,
}));
jest.mock("@/services/accessRequest.service", () => ({
  resendAccessRequestVerification: jest.fn(),
}));

const mockReenviar = resendAccessRequestVerification as jest.MockedFunction<
  typeof resendAccessRequestVerification
>;

const COOLDOWN = 60;

function setupUser() {
  return userEvent.setup({ delay: null, advanceTimers: jest.advanceTimersByTime });
}

/**
 * Corre el cooldown entero.
 *
 * Un `advanceTimersByTime(60_000)` no alcanza: cada tick agenda el siguiente
 * desde un efecto, y los efectos corren recién cuando React flushea. Hay que
 * avanzar segundo a segundo dentro de `act`.
 */
function avanzarCooldown() {
  for (let i = 0; i < COOLDOWN; i += 1) {
    act(() => {
      jest.advanceTimersByTime(1000);
    });
  }
}

/**
 * Aprieta "Reenviar" y espera a que el handler termine.
 *
 * El `waitFor` no es un adorno: con timers falsos hay que empujarlos para que
 * resuelva la promesa del servicio, y sin eso el estado se queda en
 * `"enviando"` para siempre. `waitFor` los avanza solo hasta que el handler
 * async termina.
 */
async function clickReenviar(user: ReturnType<typeof setupUser>) {
  await user.click(botonReenviar()!);
  await waitFor(() => expect(screen.queryByText("Enviando...")).toBeNull());
}

function botonReenviar() {
  return screen.queryByRole("button", { name: /Reenviar el link de confirmación/i });
}

function salida() {
  return screen.getByRole("link", { name: /Volvé a mandar el formulario/i });
}

describe("/solicitud-enviada", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.useFakeTimers();
    searchParams = new URLSearchParams("email=ana%40gmail.com");
    mockReenviar.mockResolvedValue(undefined as never);
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  describe("el reenvío se puede volver a pedir", () => {
    test("cuando el cooldown llega a cero, el botón vuelve", async () => {
      const user = setupUser();
      render(<SolicitudEnviadaPage />);

      avanzarCooldown();
      await clickReenviar(user);
      expect(mockReenviar).toHaveBeenCalledTimes(1);

      // Confirmación del primer reenvío, con el contador corriendo de nuevo.
      expect(screen.getByText(/te reenviamos el link/i)).toBeInTheDocument();
      expect(botonReenviar()).toBeNull();

      // La promesa del contador: en N segundos vas a poder pedir otro.
      avanzarCooldown();
      expect(botonReenviar()).not.toBeNull();

      await clickReenviar(user);
      expect(mockReenviar).toHaveBeenCalledTimes(2);
    });

    test("mientras el contador corre dice cuánto falta, y al llegar a cero cumple", async () => {
      const user = setupUser();
      render(<SolicitudEnviadaPage />);

      avanzarCooldown();
      await clickReenviar(user);

      // A mitad de camino sigue anunciando la espera…
      for (let i = 0; i < 30; i += 1) {
        act(() => {
          jest.advanceTimersByTime(1000);
        });
      }
      expect(screen.getByText(/Podés pedir otro en \d+s/)).toBeInTheDocument();

      // …y al terminar no desaparece el texto dejando la nada: aparece el botón.
      for (let i = 0; i < 30; i += 1) {
        act(() => {
          jest.advanceTimersByTime(1000);
        });
      }
      expect(screen.queryByText(/Podés pedir otro en/)).toBeNull();
      expect(botonReenviar()).not.toBeNull();
    });
  });

  describe("la salida honesta está en todas las ramas", () => {
    test("recién llegado, con el cooldown corriendo", () => {
      render(<SolicitudEnviadaPage />);
      expect(salida()).toHaveAttribute("href", "/solicitar-acceso");
    });

    test("con el botón de reenvío disponible", () => {
      render(<SolicitudEnviadaPage />);
      avanzarCooldown();
      expect(salida()).toHaveAttribute("href", "/solicitar-acceso");
    });

    test("después de un reenvío exitoso", async () => {
      const user = setupUser();
      render(<SolicitudEnviadaPage />);
      avanzarCooldown();
      await clickReenviar(user);

      expect(salida()).toHaveAttribute("href", "/solicitar-acceso");
    });

    test("cuando el reenvío falla", async () => {
      mockReenviar.mockRejectedValue(new Error("500"));
      const user = setupUser();
      render(<SolicitudEnviadaPage />);
      avanzarCooldown();
      await clickReenviar(user);

      expect(screen.getByText(/No pudimos reenviarlo/i)).toBeInTheDocument();
      expect(salida()).toHaveAttribute("href", "/solicitar-acceso");
    });
  });

  describe("sin `?email` en la URL", () => {
    beforeEach(() => {
      searchParams = new URLSearchParams();
    });

    test("no deja un botón muerto: explica por qué y ofrece la salida", () => {
      render(<SolicitudEnviadaPage />);
      avanzarCooldown();

      // El reenvío necesita una dirección; un botón gris para siempre y sin
      // motivo es peor que no tenerlo.
      expect(botonReenviar()).toBeNull();
      expect(
        screen.getByText(/necesitamos saber a qué dirección/i),
      ).toBeInTheDocument();
      expect(salida()).toHaveAttribute("href", "/solicitar-acceso");
    });
  });
});
