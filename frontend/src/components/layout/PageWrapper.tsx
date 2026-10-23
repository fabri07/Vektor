interface PageWrapperProps {
  title: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}

export function PageWrapper({ title, actions, children }: PageWrapperProps) {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-vk-text-primary">{title}</h1>
        {actions && <div className="flex items-center gap-2">{actions}</div>}
      </div>
      {children}
    </div>
  );
}
