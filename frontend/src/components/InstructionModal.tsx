import { X, HelpCircle, Cpu, Terminal, FileText, Code } from "lucide-react";

interface InstructionModalProps {
    onClose: () => void;
}

export default function InstructionModal({ onClose }: InstructionModalProps) {
    return (
        <div className="modal-overlay" onClick={onClose}>
            <div className="modal instruction-modal" onClick={(e) => e.stopPropagation()}>
                <div className="modal-header">
                    <h2><HelpCircle size={20} style={{ verticalAlign: 'middle', marginRight: '8px' }} /> 使用快速指引</h2>
                    <button className="btn btn-ghost" onClick={onClose}>
                        <X size={20} />
                    </button>
                </div>

                <div className="modal-body custom-scrollbar" style={{ maxHeight: '70vh', overflowY: 'auto', paddingRight: '1rem' }}>

                    <section className="ins-section">
                        <h3><Cpu size={18} /> 1. 如何申请资源</h3>
                        <p>在 <strong>资源看板</strong> 页面点击右上角的 <strong>“申请 GPU 容器”</strong> 按钮。在弹窗中选择你需要的 GPU 卡号（支持多选）和租期。申请成功后，系统会自动为你创建并启动容器。</p>
                    </section>

                    <section className="ins-section">
                        <h3><Terminal size={18} /> 2. 如何通过 SSH 连接</h3>
                        <p>申请成功后，在 <strong>我的容器</strong> 页面可以找到你正在运行的容器。点击 <strong>“复制 SSH 命令”</strong>，然后在你本地电脑的终端（CMD, PowerShell 或 Terminal）中点击右键粘贴并回车。初次连接需输入初始密码（同样在页面可见）。</p>
                    </section>

                    <section className="ins-section">
                        <h3><FileText size={18} /> 3. 如何查看和传输文件</h3>
                        <ul>
                            <li><strong>在线查看：</strong>在 <strong>我的容器</strong> 点击 <strong>“工作区”</strong> 按钮，可以使用内置的在线代码编辑器直接查看和编辑文件。</li>
                            <li><strong>本地传输：</strong>建议使用 <strong>WinSCP</strong> 或 <strong>FileZilla</strong>。连接协议选择 SFTP，主机名为服务器 IP，端口和密码与 SSH 一致。你的个人数据持久化保存于 <code>/workspace</code>。</li>
                        </ul>
                    </section>

                    <section className="ins-section">
                        <h3><Code size={18} /> 4. 如何使用 Code-Server (在线 VSCode)</h3>
                        <p>系统默认在容器内预装了 Web 版 VSCode。在 <strong>我的容器</strong> 页面，点击 <strong>“打开 VS Code”</strong> 按钮，即可在浏览器中通过熟悉的 VSCode 界面进行开发。环境已经配置好 Conda ,创建好的环境会持久化在个人目录中，后续申请使用不会丢失。</p>
                    </section>

                    <div className="modal-hint" style={{ marginTop: '1.5rem', background: 'rgba(52, 211, 153, 0.1)', borderColor: 'rgba(52, 211, 153, 0.2)', color: '#34d399' }}>
                        提示：所有在 <code>/workspace</code> 目录下的修改都会持久化保存。即使容器到期销毁，下次租用时你的代码和数据依然存在。
                    </div>
                </div>

                <div className="modal-actions">
                    <button className="btn btn-primary" onClick={onClose}>知道了</button>
                </div>
            </div>
        </div>
    );
}
