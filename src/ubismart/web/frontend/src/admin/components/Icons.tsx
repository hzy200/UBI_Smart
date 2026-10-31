interface IconProps {
  name:
    | 'users'
    | 'usage'
    | 'provider'
    | 'search'
    | 'plus'
    | 'refresh'
    | 'logout'
    | 'key'
    | 'close'
    | 'check'
    | 'warning'
    | 'eye'
    | 'eye-off'
    | 'back'
    | 'download'
    | 'folder'
    | 'shield'
    | 'database'
    | 'menu';
  className?: string;
}

const PATHS: Record<IconProps['name'], string[]> = {
  users: [
    'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2',
    'M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z',
    'M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75',
  ],
  usage: [
    'M3 3v18h18',
    'M7 15v3M12 10v8M17 6v12',
  ],
  provider: [
    'M12 2 3 7v10l9 5 9-5V7l-9-5Z',
    'M3 7l9 5 9-5M12 22V12',
  ],
  search: ['M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z', 'm21 21-4.35-4.35'],
  plus: ['M12 5v14M5 12h14'],
  refresh: ['M20 11a8 8 0 1 0-2.34 5.66', 'M20 5v6h-6'],
  logout: ['M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4', 'm16 17 5-5-5-5M21 12H9'],
  key: [
    'M21 2 19 4M15.5 7.5 19 4l2 2-3.5 3.5',
    'M10.5 13.5a5 5 0 1 1-7-7 5 5 0 0 1 7 7Zm0 0L19 4',
  ],
  close: ['M6 6l12 12M18 6 6 18'],
  check: ['m5 12 4 4L19 6'],
  warning: ['M12 3 2.5 20h19L12 3Z', 'M12 9v5M12 17.5h.01'],
  eye: ['M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z', 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z'],
  'eye-off': [
    'M3 3l18 18',
    'M10.6 10.6a3 3 0 0 0 4.2 4.2',
    'M9.9 5.2A10.5 10.5 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.2 4.1',
    'M6.1 6.1C3.5 8 2 12 2 12s3.5 7 10 7c1.4 0 2.7-.3 3.9-.8',
  ],
  back: ['M19 12H5', 'm12 19-7-7 7-7'],
  download: ['M12 3v12', 'm7 10 5 5 5-5', 'M5 21h14'],
  folder: [
    'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z',
  ],
  shield: ['M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z', 'm9 12 2 2 4-4'],
  database: [
    'M12 8c5 0 9-1.3 9-3s-4-3-9-3-9 1.3-9 3 4 3 9 3Z',
    'M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5',
    'M3 12c0 1.7 4 3 9 3s9-1.3 9-3',
  ],
  menu: ['M4 6h16M4 12h16M4 18h16'],
};

export function Icon({ name, className = 'icon' }: IconProps) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {PATHS[name].map((path) => (
        <path key={path} d={path} />
      ))}
    </svg>
  );
}
