// script.js
function addTask() {
    // 假设这里有一个添加任务的功能实现
}

function showTasks() {
    const tasks = document.getElementById('tasksList');
    // 清空列表内容
    while (tasks.firstChild) {
        tasks.removeChild(tasks.firstChild);
    }

    // 从 localStorage 中获取所有待办事项并显示在页面上
    let taskItems = JSON.parse(localStorage.getItem('taskList')) || [];
    for (let i = 0; i < taskItems.length; i++) {
        const li = document.createElement('li');
        li.textContent = taskItems[i];
        tasks.appendChild(li);
    }
}
