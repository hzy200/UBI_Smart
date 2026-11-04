/* Alpine mixin: 每用户资料库。 */

import {
  authFetch,
  readJson,
  uploadForm,
} from '../api/http';
import { uploadPercent } from '../ui/upload';
import {
  libraryDetailRows,
  libraryItemRows,
  libraryResultRows,
  libraryStageKey,
  libraryStatusKey,
  libraryUsageText,
} from '../ui/library';

const POLL_MS = 1500;

export const libraryMixin = {
  async loadLibrary() {
    await this._loadLibrary(false);
  },

  async _loadLibrary(silent) {
    if (!this.capabilities.library_enabled) {
      this.libraryItems = [];
      this.libraryUsage = '';
      this._stopLibraryPolling();
      return;
    }
    if (!this.librarySearchMode) {
      this.librarySearchMode = this.capabilities.library_search_mode || '';
    }
    try {
      const params = new URLSearchParams({
        limit: String(this.libraryLimit || 20),
        offset: String(this.libraryOffset || 0),
      });
      if (this.libraryStatusFilter) {
        params.set('status', this.libraryStatusFilter);
      }
      if (this.libraryTagFilter) {
        params.set('tag', this.libraryTagFilter);
      }
      if (this.libraryQueryFilter) {
        params.set('q', this.libraryQueryFilter);
      }
      const response = await authFetch(
        `/api/library/items?${params.toString()}`,
      );
      const data = await readJson(response, this.t('library.loadFailed'));
      this.libraryItems = libraryItemRows(
        data.items || [],
        (value) => this.formatBytes(value),
        (status) => this.t(libraryStatusKey(status)),
      ).map((item) => ({
        ...item,
        _stageLabel: item._stage
          ? this.t(libraryStageKey(item._stage))
          : '',
      }));
      this.libraryTotal = Number(data.total || this.libraryItems.length);
      this.libraryOffset = Number(data.offset || 0);
      this.libraryUsage = libraryUsageText(
        data.usage || {},
        (value) => this.formatBytes(value),
      );
      this.libraryError = '';
      this._syncLibraryPolling();
    } catch (err) {
      this.libraryError = String(err);
      if (!silent) this.toastMessage(String(err));
    }
  },

  _syncLibraryPolling() {
    const active = (this.libraryItems || []).some((item) => item._active);
    if (active && !this._libraryPollTimer) {
      this._libraryPollTimer = setInterval(() => {
        void this._loadLibrary(true);
      }, POLL_MS);
      return;
    }
    if (!active) this._stopLibraryPolling();
  },

  _stopLibraryPolling() {
    if (this._libraryPollTimer) {
      clearInterval(this._libraryPollTimer);
      this._libraryPollTimer = null;
    }
  },

  applyLibraryFilters() {
    this.libraryOffset = 0;
    void this.loadLibrary();
  },

  libraryPrevPage() {
    if (this.libraryOffset <= 0) return;
    this.libraryOffset = Math.max(
      0,
      this.libraryOffset - (this.libraryLimit || 20),
    );
    void this.loadLibrary();
  },

  libraryNextPage() {
    if (
      this.libraryOffset + (this.libraryLimit || 20)
      >= this.libraryTotal
    ) return;
    this.libraryOffset += this.libraryLimit || 20;
    void this.loadLibrary();
  },

  async openLibraryDetail(itemId) {
    if (!itemId) return;
    if (this.libraryDetailId === itemId) {
      this.closeLibraryDetail();
      return;
    }
    this.libraryDetailId = itemId;
    this.libraryDetailLoading = true;
    this.libraryDetailError = '';
    try {
      const response = await authFetch(
        `/api/library/items/${encodeURIComponent(itemId)}/detail`,
      );
      const data = await readJson(
        response, this.t('library.detailFailed')
      );
      const rows = libraryDetailRows(data);
      this.libraryDetail = data;
      this.libraryDetailOutline = rows.outline;
      this.libraryDetailChunks = rows.chunks;
    } catch (err) {
      this.libraryDetailError = String(err);
      this.libraryDetail = null;
      this.libraryDetailOutline = [];
      this.libraryDetailChunks = [];
    } finally {
      this.libraryDetailLoading = false;
    }
  },

  closeLibraryDetail() {
    this.libraryDetailId = '';
    this.libraryDetail = null;
    this.libraryDetailOutline = [];
    this.libraryDetailChunks = [];
    this.libraryDetailError = '';
  },

  async editLibraryTags(item) {
    if (!item || !item.item_id) return;
    const raw = window.prompt(
      this.t('library.tagsPrompt'),
      (item._tags || []).join(', '),
    );
    if (raw === null) return;
    const tags = String(raw)
      .split(',')
      .map((tag) => tag.trim())
      .filter(Boolean);
    try {
      const response = await authFetch(
        `/api/library/items/${encodeURIComponent(item.item_id)}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tags }),
        },
      );
      await readJson(response, this.t('library.tagsFailed'));
      this.toastMessage(this.t('library.tagsUpdated'));
      if (this.libraryDetailId === item.item_id) {
        this.libraryDetailId = '';
        await this.openLibraryDetail(item.item_id);
      }
      await this.loadLibrary();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async uploadLibraryFiles(event) {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    await this.uploadLibraryFileList(files);
  },

  async uploadLibraryFileList(files) {
    if (!files.length || this.libraryBusy) return;
    const form = new FormData();
    for (const file of files) form.append('files', file);
    this.libraryBusy = true;
    this.libraryPercent = 0;
    this.libraryText = this.t('library.uploading');
    this.libraryError = '';
    try {
      const response = await uploadForm('/api/library/items', form, {
        onProgress: ({ loaded, total }) => {
          const percent = uploadPercent(loaded, total);
          this.libraryPercent = percent;
          this.libraryText = this.t('library.uploadProgress', { percent });
        },
      });
      const data = await readJson(response, this.t('library.uploadFailed'));
      const items = Array.isArray(data.items) ? data.items : [];
      this.toastMessage(
        this.t('library.uploadedQueued', { count: items.length }),
      );
      this.libraryPercent = 100;
      this.libraryResults = [];
      this.librarySearched = false;
      await this.loadLibrary();
    } catch (err) {
      this.libraryError = String(err);
      this.toastMessage(String(err));
    } finally {
      this.libraryBusy = false;
      this.libraryText = '';
    }
  },

  async deleteLibraryItem(itemId) {
    if (!itemId) return;
    if (!window.confirm(this.t('library.deleteConfirm'))) return;
    try {
      const response = await authFetch(
        `/api/library/items/${encodeURIComponent(itemId)}`,
        { method: 'DELETE' },
      );
      await readJson(response, this.t('library.deleteFailed'));
      this.libraryResults = [];
      this.librarySearched = false;
      await this.loadLibrary();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async reindexLibraryItem(itemId) {
    if (!itemId) return;
    try {
      const response = await authFetch(
        `/api/library/items/${encodeURIComponent(itemId)}/reindex`,
        { method: 'POST' },
      );
      await readJson(response, this.t('library.reindexFailed'));
      this.libraryResults = [];
      this.librarySearched = false;
      await this.loadLibrary();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async reindexAllLibrary() {
    try {
      const response = await authFetch('/api/library/reindex-all', {
        method: 'POST',
      });
      const data = await readJson(response, this.t('library.reindexFailed'));
      const total = Number((data.job && data.job.total) || 0);
      this.toastMessage(
        this.t('library.reindexAllQueued', { count: total }),
      );
      await this.loadLibrary();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async cancelLibraryJob(jobId) {
    if (!jobId) return;
    try {
      const response = await authFetch(
        `/api/library/jobs/${encodeURIComponent(jobId)}/cancel`,
        { method: 'POST' },
      );
      await readJson(response, this.t('library.cancelFailed'));
      this.toastMessage(this.t('library.cancelQueued'));
      await this.loadLibrary();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async searchLibrary() {
    const query = String(this.libraryQuery || '').trim();
    if (!query || this.librarySearchBusy) return;
    this.librarySearchBusy = true;
    this.librarySearchError = '';
    try {
      const params = new URLSearchParams({ q: query, top_k: '8' });
      if (this.librarySearchMode) {
        params.set('mode', this.librarySearchMode);
      }
      if (this.libraryTagFilter) {
        params.set('tags', this.libraryTagFilter);
      }
      const response = await authFetch(
        `/api/library/search?${params.toString()}`,
      );
      const data = await readJson(response, this.t('library.searchFailed'));
      this.libraryResults = libraryResultRows(data.hits || []);
      this.librarySearched = true;
    } catch (err) {
      this.librarySearchError = String(err);
      this.libraryResults = [];
      this.librarySearched = false;
    } finally {
      this.librarySearchBusy = false;
    }
  },
};
