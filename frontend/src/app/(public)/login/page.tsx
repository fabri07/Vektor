export default function LoginPage() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-primary px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <span className="text-2xl font-bold tracking-tight text-white">
            Véktor
          </span>
          <p className="mt-1 text-sm text-white/50">Salud financiera para tu negocio</p>
        </div>
        <div className="rounded-xl border border-white/10 bg-white/5 p-8">
          <h1 className="mb-6 text-lg font-semibold text-white">Iniciar sesión</h1>
          {/* TODO: LoginForm feature component */}
          <p className="text-sm text-white/40">Formulario de login — próximamente</p>
        </div>
      </div>
    </main>
  );
}
