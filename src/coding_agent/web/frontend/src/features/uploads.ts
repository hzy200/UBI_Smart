/* Alpine mixin: uploads。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
  newIdempotencyKey,
  readJson,
  uploadForm,
} from '../api/http';
import { uploadPercent } from '../ui/upload';

export const uploadsMixin = {
  async uploadFiles(event) {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    await this.uploadFileList(files);
  },

  async uploadFileList(files) {
    if (!files.length || this.uploadBusy) return;
    try {
      if (!this.threadId) await this.createSession();
    } catch (err) {
      this.toastMessage(String(err));
      return;
    }
    if (!this.threadId) return;
    const form = new FormData();
    for (const file of files) form.append('files', file);
    this._lastUploadFiles = files;
    this.uploadBusy = true;
    this.uploadFailed = false;
    this.uploadPercent = 0;
    this.uploadText = this.t('composer.uploading');
    const controller = new AbortController();
    this._uploadAbort = controller;
    try {
      const response = await uploadForm(
        `/api/sessions/${encodeURIComponent(this.threadId)}/uploads`,
        form,
        {
          headers: { 'Idempotency-Key': newIdempotencyKey() },
          signal: controller.signal,
          onProgress: ({ loaded, total }) => {
            const percent = uploadPercent(loaded, total);
            this.uploadPercent = percent;
            this.uploadText = this.t('composer.uploadProgress', { percent });
          },
        },
      );
      const data = await readJson(response, '上传失败');
      const uploaded = Array.isArray(data.uploads) ? data.uploads : [];
      const paths = uploaded.map((item) => item.relative_path).join('\n');
      this.pushMessage('system', `${this.t('composer.uploadedTo')}\n${paths}`);
      this.toastMessage(
        this.t('composer.uploadedCount', { count: uploaded.length }),
      );
      this.uploadPercent = 100;
      await this.loadUploads();
    } catch (err) {
      if (err && err.name === 'AbortError') {
        this.toastMessage(this.t('composer.uploadCancelled'));
      } else {
        this.uploadFailed = true;
        this.toastMessage(String(err));
      }
    } finally {
      this.uploadBusy = false;
      this.uploadText = '';
      if (this._uploadAbort === controller) this._uploadAbort = null;
    }
  },

  cancelUpload() {
    if (this._uploadAbort) this._uploadAbort.abort();
  },

  retryUpload() {
    if (this.uploadBusy) return;
    const files = this._lastUploadFiles || [];
    if (files.length) void this.uploadFileList(files);
  },

  async loadUploads() {
    if (!this.threadId) {
      this.uploads = [];
      this.uploadQuota = '';
      return;
    }
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(this.threadId)}/uploads`,
      );
      const data = await readJson(response, '读取上传列表失败');
      this.uploads = (data.uploads || []).map((item) => ({
        ...item,
        _name: item.relative_path.split('/').pop(),
        _size: this.formatBytes(item.bytes),
      }));
      this.uploadQuota =
        `本会话 ${this.formatBytes(data.used_bytes)}` +
        ` · 工作区 ${this.formatBytes(data.workspace_bytes)}` +
        ` / ${this.formatBytes(data.max_total_bytes)}`;
    } catch (err) {
      this.uploadQuota = '';
    }
  },

  async deleteUpload(relativePath) {
    if (!this.threadId) return;
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(this.threadId)}/uploads`,
        {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: relativePath }),
        },
      );
      await readJson(response, '删除上传失败');
      await this.loadUploads();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async dropFiles(event) {
    await this.uploadFileList(Array.from(event.dataTransfer.files || []));
  },

};
