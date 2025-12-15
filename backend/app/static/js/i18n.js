/**
 * Client-side translations
 * Synchronized with backend i18n.py
 */

const translations = {
    ru: {
        // Common
        loading: "Загрузка...",
        save: "Сохранить",
        cancel: "Отмена",
        delete: "Удалить",
        edit: "Редактировать",
        add: "Добавить",
        search: "Поиск",
        filter: "Фильтр",
        actions: "Действия",
        status: "Статус",
        name: "Название",
        description: "Описание",
        success: "Успешно",
        error: "Ошибка",
        warning: "Предупреждение",
        confirm: "Подтвердить",
        yes: "Да",
        no: "Нет",
        
        // Messages
        profile_updated: "Профиль обновлён",
        password_changed: "Пароль успешно изменён",
        settings_saved: "Настройки сохранены",
        language_changed: "Язык изменён. Страница будет перезагружена...",
        error_occurred: "Произошла ошибка",
        confirm_delete: "Вы уверены, что хотите удалить?",
    },
    
    en: {
        // Common
        loading: "Loading...",
        save: "Save",
        cancel: "Cancel",
        delete: "Delete",
        edit: "Edit",
        add: "Add",
        search: "Search",
        filter: "Filter",
        actions: "Actions",
        status: "Status",
        name: "Name",
        description: "Description",
        success: "Success",
        error: "Error",
        warning: "Warning",
        confirm: "Confirm",
        yes: "Yes",
        no: "No",
        
        // Messages
        profile_updated: "Profile updated",
        password_changed: "Password changed successfully",
        settings_saved: "Settings saved",
        language_changed: "Language changed. Page will reload...",
        error_occurred: "An error occurred",
        confirm_delete: "Are you sure you want to delete?",
    }
};

// Get current language from localStorage or default
function getCurrentLanguage() {
    return localStorage.getItem('panel_language') || 'ru';
}

// Set language
function setLanguage(lang) {
    localStorage.setItem('panel_language', lang);
}

// Get translation
function t(key, lang = null) {
    if (!lang) {
        lang = getCurrentLanguage();
    }
    
    if (translations[lang] && translations[lang][key]) {
        return translations[lang][key];
    }
    
    // Fallback to Russian
    if (translations.ru[key]) {
        return translations.ru[key];
    }
    
    return key;
}

// Export for use in other scripts
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { translations, t, getCurrentLanguage, setLanguage };
}
