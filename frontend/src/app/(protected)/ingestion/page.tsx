import { Suspense } from "react";

import { PageWrapper } from "@/components/layout/PageWrapper";
import { IngestionPage } from "@/features/ingestion/IngestionPage";

/**
 * `/ingestion?file=<id>` abre ese archivo expandido — es como aterriza el
 * usuario que viene del final del onboarding a revisar el mapeo de columnas.
 * El `Suspense` es obligatorio: `useSearchParams()` sin límite rompe el build
 * estático de Next.
 */
export default function IngestionRoute() {
  return (
    <PageWrapper title="Carga de datos">
      <Suspense fallback={null}>
        <IngestionPage />
      </Suspense>
    </PageWrapper>
  );
}
