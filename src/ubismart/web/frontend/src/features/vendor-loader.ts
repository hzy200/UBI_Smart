/* 按需加载本地 vendored viewer 库。 */

type VendorName = 'mammoth' | 'xlsx' | 'pdfjs';

const VENDOR: Record<VendorName, { src: string; integrity: string }> = {
  mammoth: {
    src: '/static/vendor/mammoth.browser.min.js',
    integrity:
      'sha384-/cXAMbzovUIKbBERjPmR3SnPTh8siWr5lsvFYj1Uq4XP0yaJUZJmsh0YXyGv5P0y',
  },
  xlsx: {
    src: '/static/vendor/xlsx.full.min.js',
    integrity:
      'sha384-vtjasyidUo0kW94K5MXDXntzOJpQgBKXmE7e2Ga4LG0skTTLeBi97eFAXsqewJjw',
  },
  pdfjs: {
    src: '/static/vendor/pdf.min.js',
    integrity:
      'sha384-/1qUCSGwTur9vjf/z9lmu/eCUYbpOTgSjmpbMQZ1/CtX2v/WcAIKqRv+U1DUCG6e',
  },
};

const pending: Partial<Record<VendorName, Promise<void>>> = {};

export function loadVendor(name: VendorName): Promise<void> {
  if (pending[name]) return pending[name] as Promise<void>;
  const entry = VENDOR[name];
  const promise = new Promise<void>((resolve, reject) => {
    const script = document.createElement('script');
    script.src = entry.src;
    script.integrity = entry.integrity;
    script.async = true;
    script.onload = () => {
      if (name === 'pdfjs' && window.pdfjsLib) {
        window.pdfjsLib.GlobalWorkerOptions.workerSrc =
          '/static/vendor/pdf.worker.min.js';
      }
      resolve();
    };
    script.onerror = () => reject(new Error(`加载 ${entry.src} 失败`));
    document.head.appendChild(script);
  });
  pending[name] = promise;
  promise.catch(() => {
    delete pending[name];
  });
  return promise;
}
