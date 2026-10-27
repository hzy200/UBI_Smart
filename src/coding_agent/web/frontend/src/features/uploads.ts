/* Alpine mixin: uploads。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
  newIdempotencyKey,
  readJson,
} from '../api/http';

export const uploadsMixin = {
  async uploadFiles(event) {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    await this.uploadFileList(files);
  },

  async uploadFileList(files) {
    if (!files.length) return;
    try {
      if (!this.threadId) await this.createSession();
      const form = new FormData();
      for (const file of files) form.append('files', file);
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(this.threadId)}/uploads`,
        {
          method: 'POST',
          body: form,
          headers: { 'Idempotency-Key': newIdempotencyKey() },
        },
      );
      const data = await readJson(response, '上传失败');
      const paths = (data.uploads || []).map((item) => item.relative_path).join('\n');
      this.pushMessage('system', `已上传到工作区：\n${paths}`);
      this.toastMessage(`已上传 ${data.uploads.length} 个文件`);
      await this.loadUploads();
    } catch (err) {
      this.toastMessage(String(err));
    }
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
