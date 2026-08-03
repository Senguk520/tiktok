import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'

import App from './App.vue'
import { installApiErrorHandler } from './api/client'
import router from './router'
import './styles.css'

installApiErrorHandler(router)

createApp(App).use(ElementPlus).use(router).mount('#app')