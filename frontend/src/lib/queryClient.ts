import { QueryClient } from "@tanstack/react-query";

import { reintentarSalvoBloqueo } from "@/lib/subscription";
import { esPinCancelado } from "@/stores/pinGateStore";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000 * 60 * 2,       // 2 minutes
      gcTime: 1000 * 60 * 10,          // 10 minutes
      // Un 402 de análisis bloqueado no se arregla reintentando (B14). Un PIN
      // cancelado tampoco: reintentar le volvía a abrir el modal al que acaba
      // de cerrarlo.
      retry: (fallas, error) => !esPinCancelado(error) && reintentarSalvoBloqueo(fallas, error),
      refetchOnWindowFocus: false,
    },
    mutations: {
      retry: 0,
    },
  },
});
